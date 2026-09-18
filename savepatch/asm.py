"""Tiny LR35902 (Game Boy CPU) assembler.

Only supports the subset of instructions used by the Hermano save patch, but it
resolves labels, so the patch can be maintained as readable source instead of
hand-encoded bytes. Two passes: pass 1 sizes every line, pass 2 emits.
"""
import re

R8 = {'b': 0, 'c': 1, 'd': 2, 'e': 3, 'h': 4, 'l': 5, '(hl)': 6, 'a': 7}
R16 = {'bc': 0, 'de': 1, 'hl': 2, 'sp': 3}
CC = {'nz': 0, 'z': 1, 'nc': 2, 'c': 3}


class AsmError(Exception):
    pass


def _clean(line):
    # strip comments, but not inside a string literal
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        if ch == ';' and not in_str:
            break
        out.append(ch)
    return ''.join(out).strip()


class Assembler:
    def __init__(self):
        self.consts = {}
        self.labels = {}
        self.final = False

    def expr(self, s, pc=None):
        """Evaluate a numeric expression: $hex, 0xhex, decimal, label, a+b, a-b."""
        s = s.strip()
        if not s:
            raise AsmError('empty expression')
        total, sign, i = 0, 1, 0
        for tok in re.split(r'([+-])', s):
            tok = tok.strip()
            if tok == '+':
                sign = 1
                continue
            if tok == '-':
                sign = -1
                continue
            if not tok:
                continue
            if tok.startswith('$'):
                v = int(tok[1:], 16)
            elif tok.lower().startswith('0x'):
                v = int(tok[2:], 16)
            elif re.fullmatch(r'-?\d+', tok):
                v = int(tok)
            elif tok in self.consts:
                v = self.consts[tok]
            elif tok in self.labels:
                v = self.labels[tok]
            elif not self.final:
                v = 0          # pass 1: forward reference, size is fixed anyway
            else:
                raise AsmError(f'undefined symbol: {tok!r}')
            total += sign * v
            sign = 1
        return total

    # ---- instruction encoders -------------------------------------------------
    def encode(self, mnem, arg, pc, final):
        a = (arg or '').strip()
        parts = [p.strip() for p in a.split(',')] if a else []

        def n8(x):
            v = self.expr(x) & 0xFF
            return [v]

        def n16(x):
            v = self.expr(x) & 0xFFFF
            return [v & 0xFF, v >> 8]

        def mem(x):
            return x.startswith('(') and x.endswith(')')

        def inner(x):
            return x[1:-1].strip()

        if mnem == 'db':
            out = []
            for item in re.findall(r'"[^"]*"|[^,]+', a):
                item = item.strip()
                if item.startswith('"'):
                    out += list(item[1:-1].encode('ascii'))
                else:
                    out.append(self.expr(item) & 0xFF)
            return out
        if mnem == 'nop':
            return [0x00]
        if mnem == 'ret':
            return [0xC9] if not parts else [0xC0 | (CC[parts[0]] << 3)]
        if mnem == 'reti':
            return [0xD9]
        if mnem == 'di':
            return [0xF3]
        if mnem == 'ei':
            return [0xFB]
        if mnem == 'xor' and parts[0] in R8:
            return [0xA8 | R8[parts[0]]]
        if mnem == 'or' and parts[0] in R8:
            return [0xB0 | R8[parts[0]]]
        if mnem == 'and':
            return [0xA0 | R8[parts[0]]] if parts[0] in R8 else [0xE6] + n8(parts[0])
        if mnem == 'cp':
            return [0xB8 | R8[parts[0]]] if parts[0] in R8 else [0xFE] + n8(parts[0])
        if mnem == 'add':
            if len(parts) == 2 and parts[0] == 'a':
                return [0x80 | R8[parts[1]]] if parts[1] in R8 else [0xC6] + n8(parts[1])
            if len(parts) == 2 and parts[0] == 'hl':
                return [0x09 | (R16[parts[1]] << 4)]
        if mnem == 'inc':
            if parts[0] in R16:
                return [0x03 | (R16[parts[0]] << 4)]
            return [0x04 | (R8[parts[0]] << 3)]
        if mnem == 'dec':
            if parts[0] in R16:
                return [0x0B | (R16[parts[0]] << 4)]
            return [0x05 | (R8[parts[0]] << 3)]
        if mnem == 'push':
            return [0xC5 | ({'bc': 0, 'de': 1, 'hl': 2, 'af': 3}[parts[0]] << 4)]
        if mnem == 'pop':
            return [0xC1 | ({'bc': 0, 'de': 1, 'hl': 2, 'af': 3}[parts[0]] << 4)]
        if mnem == 'call':
            if len(parts) == 1:
                return [0xCD] + n16(parts[0])
            return [0xC4 | (CC[parts[0]] << 3)] + n16(parts[1])
        if mnem == 'jp':
            if len(parts) == 1:
                return [0xE9] if parts[0] == 'hl' else [0xC3] + n16(parts[0])
            return [0xC2 | (CC[parts[0]] << 3)] + n16(parts[1])
        if mnem == 'jr':
            target = self.expr(parts[-1]) if final else pc + 2
            off = target - (pc + 2)
            if final and not -128 <= off <= 127:
                raise AsmError(f'jr out of range at {pc:#06x}: {off}')
            off &= 0xFF
            return ([0x18] if len(parts) == 1 else [0x20 | (CC[parts[0]] << 3)]) + [off]
        if mnem == 'ldh':
            # ldh a,($xx) / ldh ($xx),a - the $FF00-page accessors
            dst, src = parts[0], parts[1]
            if dst == 'a' and mem(src):
                return [0xF0] + n8(inner(src))
            if mem(dst) and src == 'a':
                return [0xE0] + n8(inner(dst))
            raise AsmError(f'unsupported ldh: {a}')
        if mnem == 'ld':
            dst, src = parts[0], parts[1]
            # 16-bit immediate loads
            if dst in R16 and not mem(src):
                return [0x01 | (R16[dst] << 4)] + n16(src)
            # (hl+)/(hl-) forms
            if dst in ('(hl+)', '(hli)') and src == 'a':
                return [0x22]
            if dst in ('(hl-)', '(hld)') and src == 'a':
                return [0x32]
            if dst == 'a' and src in ('(hl+)', '(hli)'):
                return [0x2A]
            if dst == 'a' and src in ('(hl-)', '(hld)'):
                return [0x3A]
            if dst == '(bc)' and src == 'a':
                return [0x02]
            if dst == '(de)' and src == 'a':
                return [0x12]
            if dst == 'a' and src == '(bc)':
                return [0x0A]
            if dst == 'a' and src == '(de)':
                return [0x1A]
            # register-to-register (covers the (hl) pseudo-register)
            if dst in R8 and src in R8:
                return [0x40 | (R8[dst] << 3) | R8[src]]
            # absolute memory
            if mem(dst) and src == 'a':
                return [0xEA] + n16(inner(dst))
            if dst == 'a' and mem(src):
                return [0xFA] + n16(inner(src))
            # immediate
            if dst in R8:
                return [0x06 | (R8[dst] << 3)] + n8(src)
        raise AsmError(f'unsupported instruction: {mnem} {a}')

    # ---- driver ---------------------------------------------------------------
    def assemble(self, text, org=0):
        lines = []
        for raw in text.splitlines():
            s = _clean(raw)
            if not s:
                continue
            m = re.match(r'^(\w+)\s*=\s*(.+)$', s)
            if m:
                self.consts[m.group(1)] = self.expr(m.group(2))
                continue
            while True:
                m = re.match(r'^([A-Za-z_]\w*):\s*(.*)$', s)
                if not m:
                    break
                lines.append(('label', m.group(1)))
                s = m.group(2).strip()
                if not s:
                    break
            if not s:
                continue
            m = re.match(r'^(\.?\w+)\s*(.*)$', s)
            if not m:
                raise AsmError(f'cannot parse: {s!r}')
            lines.append(('op', m.group(1).lower().lstrip('.'), m.group(2)))

        for final in (False, True):
            self.final = final
            pc, out = org, []
            for item in lines:
                if item[0] == 'label':
                    if not final:
                        self.labels[item[1]] = pc
                    elif self.labels[item[1]] != pc:
                        raise AsmError(f'label moved: {item[1]}')
                    continue
                b = self.encode(item[1], item[2], pc, final)
                pc += len(b)
                if final:
                    out += b
        return bytes(out)


def assemble(text, org=0):
    a = Assembler()
    code = a.assemble(text, org)
    return code, a.labels, a.consts
