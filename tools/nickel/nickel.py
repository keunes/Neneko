"""Read Kobo's libnickel (32-bit ARM, Thumb-2) for the parts Neneko depends on.

Bring your own copy of the library from your Kobo (/usr/local/Kobo/libnickel.so.1.0.0); nothing
of Kobo's is included here. See README.md for the commands behind docs/kobo-notes.md.

    python -I nickel.py LIB dis 'FeatureSettings::maxBooksToDownload'
    python -I nickel.py LIB dis 'ResolveEntitlementUpdatesCommand::downloadRecentBooks' --calls
    python -I nickel.py LIB callers 'LibraryParser::numRecentBooksFirst'
    python -I nickel.py LIB strref x-kobo-recent-reads

Needs capstone and pyelftools (requirements.txt) and c++filt (binutils).
"""

import argparse
import bisect
import hashlib
import io
import os
import pickle
import subprocess
import sys
from pathlib import Path

from capstone import CS_ARCH_ARM, CS_MODE_ARM, CS_MODE_THUMB, Cs
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_OP_REG, ARM_REG_PC
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection


class Library:
    def __init__(self, path: Path, cache_dir: Path | None):
        self.data = path.read_bytes()
        self.elf = ELFFile(io.BytesIO(self.data))
        self.segments = [(s["p_vaddr"], s["p_filesz"], s["p_offset"])
                         for s in self.elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        cache = cache_dir / f"{hashlib.sha256(self.data).hexdigest()[:16]}.pickle" if cache_dir else None
        if cache and cache.exists():
            self.funcs, self.names, self.got = pickle.loads(cache.read_bytes())
        else:
            self.funcs, self.names, self.got = self._symbols()
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_bytes(pickle.dumps((self.funcs, self.names, self.got)))
        self.starts = [address for address, _ in self.funcs]

    # memory

    def read(self, address: int, size: int) -> bytes | None:
        for vaddr, filesz, offset in self.segments:
            if vaddr <= address < vaddr + filesz:
                return self.data[offset + address - vaddr: offset + address - vaddr + size]
        return None

    def u32(self, address: int) -> int | None:
        raw = self.read(address, 4)
        return int.from_bytes(raw, "little") if raw and len(raw) == 4 else None

    def cstring(self, address: int, limit: int = 200) -> str | None:
        raw = self.read(address, limit)
        if not raw or (end := raw.find(b"\0")) <= 0:
            return None
        try:
            text = raw[:end].decode()
        except UnicodeDecodeError:
            return None
        return text if all(31 < ord(c) < 127 or c in "\n\t" for c in text) else None

    # symbols

    def _symbols(self):
        dynsym = self.elf.get_section_by_name(".dynsym")
        raw = [(s["st_value"], s.name, s["st_info"]["type"]) for s in dynsym.iter_symbols()
               if s["st_value"] and s.name]
        demangled = _demangle([name for _, name, _ in raw])
        names, funcs = {}, []
        for (value, _, kind), name in zip(raw, demangled):
            names.setdefault(value & ~1, name)
            if kind == "STT_FUNC":
                funcs.append((value & ~1, name))
        got_raw = {}
        for section in self.elf.iter_sections():
            if isinstance(section, RelocationSection):
                for rel in section.iter_relocations():
                    if rel["r_info_sym"]:
                        got_raw[rel["r_offset"]] = dynsym.get_symbol(rel["r_info_sym"]).name
        unique = sorted(set(got_raw.values()))
        readable = dict(zip(unique, _demangle(unique)))
        got = {address: readable[name] for address, name in got_raw.items()}
        # PLT stubs (ARM mode): add ip, pc, #a; add ip, ip, #b; ldr pc, [ip, #c]!
        plt = self.elf.get_section_by_name(".plt")
        arm = Cs(CS_ARCH_ARM, CS_MODE_ARM)
        arm.detail = True
        address, end = plt["sh_addr"] + 20, plt["sh_addr"] + plt["sh_size"]
        while address < end:
            stub = list(arm.disasm(self.read(address, 12), address))
            if len(stub) == 3 and stub[0].mnemonic == "add" and stub[2].mnemonic == "ldr":
                slot = address + 8 + _arm_immediate(stub[0]) + _arm_immediate(stub[1]) + stub[2].operands[1].mem.disp
                names[address] = "PLT:" + got.get(slot, hex(slot))
                funcs.append((address, names[address]))
                address += 12
            else:
                address += 4
        funcs.sort()
        return funcs, names, got

    def owner(self, address: int) -> tuple[int, str]:
        index = bisect.bisect_right(self.starts, address) - 1
        return self.funcs[index] if index >= 0 else (0, "?")

    def find(self, query: str) -> list[tuple[int, str]]:
        if query.startswith("0x"):
            address = int(query, 16) & ~1
            return [(address, self.owner(address)[1])]
        return [(a, n) for a, n in self.funcs if query in n and not n.startswith("PLT:")]

    # code

    def disassemble(self, start: int, limit: int, calls_only: bool = False) -> None:
        index = bisect.bisect_right(self.starts, start)  # the next function ends this one
        end = min(self.starts[index], start + limit) if index < len(self.starts) else start + limit
        thumb = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        thumb.detail = True
        literals: dict[int, int | None] = {}  # register -> value loaded from the literal pool
        for ins in thumb.disasm(self.read(start, end - start), start):
            note = self._annotate(ins, start, end, literals)
            if calls_only and not (note or ins.mnemonic in ("bl", "blx")):
                continue
            print(f"  {ins.address:08x}: {ins.mnemonic:8s} {ins.op_str:32s} {note}")

    def _annotate(self, ins, start: int, end: int, literals: dict) -> str:
        ops = ins.operands
        if ins.mnemonic.startswith("ldr") and len(ops) == 2 and ops[1].type == ARM_OP_MEM \
                and ops[1].mem.base == ARM_REG_PC:
            value = self.u32(((ins.address + 4) & ~3) + ops[1].mem.disp)
            literals[ops[0].reg] = value
            return ""
        if ins.mnemonic.startswith("add") and len(ops) == 2 and ops[1].type == ARM_OP_REG \
                and ops[1].reg == ARM_REG_PC and literals.get(ops[0].reg) is not None:
            target = (literals.pop(ops[0].reg) + ins.address + 4) & 0xFFFFFFFF
            if target in self.got:
                return f"-> GOT {self.got[target]}"
            if text := self.cstring(target):
                return f'-> "{text}"'
            return f"-> {self.names.get(target, hex(target))}"
        if ins.mnemonic in ("bl", "blx", "b", "b.w") and ops and ops[0].type == ARM_OP_IMM:
            target = ops[0].imm
            if ins.mnemonic in ("bl", "blx") or not start <= target < end:
                return self.names.get(target & ~1) or "~" + self.owner(target)[1]
        return ""

    def callers(self, query: str) -> None:
        targets = {a: n for a, n in self.funcs if query in n}
        text = self.elf.get_section_by_name(".text")
        base, code = text["sh_addr"], text.data()
        for i in range(0, len(code) - 4, 2):
            first = int.from_bytes(code[i:i + 2], "little")
            second = int.from_bytes(code[i + 2:i + 4], "little")
            if first >> 11 != 0b11110 or second >> 14 != 0b11:
                continue
            blx = not (second >> 12) & 1
            s = (first >> 10) & 1
            i1, i2 = 1 - (((second >> 13) & 1) ^ s), 1 - (((second >> 11) & 1) ^ s)
            offset = (s << 24) | (i1 << 23) | (i2 << 22) | ((first & 0x3FF) << 12) | ((second & 0x7FF) << 1)
            if s:
                offset -= 1 << 25
            pc = base + i + 4
            target = ((pc & ~3) if blx else pc) + offset
            if target in targets:
                print(f"{base + i:08x} in {self.owner(base + i)[1]}  -> {targets[target]}")

    def string_references(self, value: str) -> None:
        """Code that loads the address of a string: ldr rX, [pc, #n] ... add rX, pc."""
        rodata = self.elf.get_section_by_name(".rodata")
        data, needle = rodata.data(), value.encode() + b"\0"
        found, at = [], data.find(needle)
        while at >= 0:
            if at == 0 or data[at - 1] == 0:
                found.append(rodata["sh_addr"] + at)
            at = data.find(needle, at + 1)
        text = self.elf.get_section_by_name(".text")
        base, code = text["sh_addr"], text.data()
        for target in found:
            for i in range(0, len(code) - 2, 2):
                half = int.from_bytes(code[i:i + 2], "little")
                if half & 0xFF78 != 0x4478:  # add rd, pc (16-bit)
                    continue
                register = (half & 7) | ((half >> 4) & 8)
                wanted = (target - (base + i + 4)) & 0xFFFFFFFF
                for j in range(i - 2, max(i - 64, 0), -2):
                    load = int.from_bytes(code[j:j + 2], "little")
                    if load >> 11 == 0b01001 and (load >> 8) & 7 == register:
                        pool = ((base + j + 4) & ~3) + (load & 0xFF) * 4
                        if self.u32(pool) == wanted:
                            print(f"{base + i:08x} in {self.owner(base + i)[1]}")
                        break


def _arm_immediate(ins) -> int:
    word = int.from_bytes(ins.bytes, "little")
    value, rotate = word & 0xFF, ((word >> 8) & 0xF) * 2
    return ((value >> rotate) | (value << (32 - rotate))) & 0xFFFFFFFF


def _demangle(names: list[str]) -> list[str]:
    out = subprocess.run(["c++filt"], input="\n".join(names), capture_output=True, text=True, check=True).stdout
    return out.split("\n")[:len(names)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("library", type=Path)
    default_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "neneko-nickel"
    parser.add_argument("--cache", type=Path, default=default_cache,
                        help="where the symbol table is cached (default: %(default)s)")
    parser.add_argument("--no-cache", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    dis = commands.add_parser("dis", help="disassemble functions whose name contains SYMBOL (or at 0xADDR)")
    dis.add_argument("symbol")
    dis.add_argument("--bytes", type=int, default=4000, help="at most this many bytes per function")
    dis.add_argument("--calls", action="store_true", help="only calls, strings and GOT references")
    callers = commands.add_parser("callers", help="call sites of functions whose name contains SYMBOL")
    callers.add_argument("symbol")
    strref = commands.add_parser("strref", help="code that loads the address of a string")
    strref.add_argument("string")
    args = parser.parse_args()

    library = Library(args.library, None if args.no_cache else args.cache)
    if args.command == "dis":
        matches = library.find(args.symbol)
        if not matches:
            sys.exit(f"no function matches {args.symbol!r}")
        for address, name in matches:
            print(f"== {address:#x} {name}")
            library.disassemble(address, args.bytes, args.calls)
    elif args.command == "callers":
        library.callers(args.symbol)
    else:
        library.string_references(args.string)


if __name__ == "__main__":
    main()
