# how it works

heres how OG360 edits memory and calls game functions in og xbox games running under xefu, on all 8 xefu versions. theres enough here to do it yourself without OG360.

you need a jtag/rgh 360 with xbdm loaded. OG360 talks to it through [py-xbdm](https://github.com/XeCrippy/py-xbdm) by [XeCrippy](https://github.com/XeCrippy).

watch the byte order. the 360 is big endian but everything on the og xbox side is little endian.

## keeping the console online

when an og game turns on its network card (halo 2 does about 8 seconds in) xefu calls xam export 109, `NetDll_XnpEthernetInterceptSetCallbacks`, with filters that take over the 360s network. xbdm goes dead until you quit the game. aurora gets around it with its nova plugin, which answers that call itself. OG360 does the same thing from the pc so it works from any dash.

1. find xams export table, just by reading memory:
   - search the first 8 KB of `xboxkrnl.exe` for `XEX2`. thats the kernels xex header. header +0x10 is the security info and security info +0x160 is the export table.
   - kernel export 403 (`XexExecutableModuleHandle`) holds a pointer to the running titles loader entry. follow the links at +0 until you hit the entry with image base (+0x1C) = xams base from the xbdm module list.
   - xams entry +0x58 is its xex header. +0x10 and +0x160 again gets you its export table.
   - in an export table +0x20 has the image base >> 16 in its low 16 bits, +0x24 is the count, +0x28 the first ordinal and the slots start at +0x2C. a slot holds the function address minus the image base.
2. write this 0x20 bytes before the next 64 KB boundary after the end of xams export table (0x81A7FFE0 on mine). check its all zeros and still inside xams code first.
   ```
   2C030001   cmpwi r3, 1
   4082000C   bne   +12
   38600000   li    r3, 0
   4E800020   blr
   48xxxxxx   b     <real export 109>
   ```
3. point slot 109 at it. a game (r3 = 1) gets told its done and never takes the network card. anything else goes on to the real function.

it has to be in place before the game starts since games grab their imports when they load. it only lives in ram so a reboot or the auto power off clears it. OG360 puts it back every time it connects. the game itself wont have network after this, same as on aurora.

## finding the games memory

xefu gives the og xbox its 64 MB of ram as one block of 360 memory and boots a real x86 xbox kernel in it. so a game address goes through normal x86 page tables that live inside that block.

1. read the emulators pe header at 0x82000000. its timestamp (pe +0x8) and entry point (pe +0x28) tell you which xefu it is. see the table at the bottom.
2. read 8 bytes at that xefus ram pointer. its the start and end of the games ram as two big endian u32s, 0x4000000 apart.
3. check you got it right. ram +0x10000 starts with `MZ` (the xbox kernel) and the u32 at ram +0xF000 +0x300*4 points back at 0xF000 (the page directory maps itself).
4. turn a game address into a 360 address:
   ```
   pde = u32 at ram + 0xF000 + (addr >> 22) * 4
   if pde & 0x80:   phys = (pde & 0xFFC00000) | (addr & 0x3FFFFF)          4 MB page
   else:            pte  = u32 at ram + (pde & 0xFFFFF000) + ((addr >> 12) & 0x3FF) * 4
                    phys = (pte & 0xFFFFF000) | (addr & 0xFFF)
   360 address = ram + phys
   ```
   bit 0 of the pde and pte has to be set or the page isnt mapped. do it for every 4 KB page, pages that are next to each other in the game arent always next to each other in ram.
5. read and write with xbdm `getmem` and `setmem`. `getmemex` sends binary back and is about 10x faster for big reads.

the games xbe header is at game address 0x10000 (`XBEH`). the title id is at [0x10118] + 8.

data edits work great like this. code edits dont, see the xefu notes at the bottom.

## calling game functions

the xbox kernel running inside xefu is real x86 and it already knows how to run something on a game thread: apcs. queue a normal apc whose `NormalRoutine` is a small gate you put in the kernels padding and the kernel calls it for you. nothing in the game gets touched so it works in every game. the kernel only changes between xefu versions, so one profile per xefu covers every game on it.

each xefu needs (see the table):
- the kpcr. irql is at +0x24, current thread +0x28, next thread +0x2C, idle thread +0x30 and an interrupt count at +0x38 that only moves while the kernel runs.
- the software interrupt request word. bit 2 asks for an apc, bit 4 for a dpc.
- a `ret 14h` in the kernel to use as the `KernelRoutine`. it does nothing.
- 7 runs of int3 or nop padding right after a `ret` or `jmp` that nothing in the kernel points to, for the gate. OG360s spots are in `og360/og/xefu/profiles`.

the gate. each piece ends with an `E9` jmp to the next padding run:
```
9C            pushfd
60            pushad
FC            cld
8B5C2428      mov  ebx, [esp+28h]     NormalContext = the request
FF7324        push [ebx+24h]          arg 6
FF7320        push [ebx+20h]
FF731C        push [ebx+1Ch]
FF7318        push [ebx+18h]
FF7314        push [ebx+14h]
FF7310        push [ebx+10h]          arg 1
FF13          call [ebx]
894308        mov  [ebx+8], eax
83C418        add  esp, 18h
FF430C        inc  [ebx+0Ch]          done counter
61            popad
9D            popfd
C20C00        ret  0Ch
```

the request is 192 bytes. put it in the zeros after the xbe headers, from 0x10000 + SizeOfHeaders (xbe +0x108) to the end of that page. check its all zeros first.

| offset | what |
|---|---|
| +0x00 | address the gate calls |
| +0x04 | arg count |
| +0x08 | eax comes back here |
| +0x0C | done counter |
| +0x10 | 6 stack args |
| +0x28 | real target, for the second gate |
| +0x2C | eax ecx edx esi edi, for the second gate |
| +0x40 | the `KAPC` |
| +0x68 | edx comes back here |
| +0x70 | float comes back here |
| +0x80 | text for string args |

a call goes like this:
1. make sure the interrupt count is moving. a paused game never runs the call.
2. send xbdm `stop`. the whole console freezes so nothing changes while you write.
3. look at the current thread. it has to be a game thread at passive irql: not the idle thread, type (+0) 6, state (+0x2C) 2, empty kernel apc list (+0x34), +0x48 and +0x49 both 0, `KernelApcDisable` (+0x68) 0, irql 0 and the apc bit not already set. if its not, `go`, wait a random 20 to 150 ms and stop again. random so you dont keep landing on the same spot in the frame.
4. write the request and the gate.
5. build the `KAPC` at request +0x40: type 0x12, mode 0, inserted 1, the thread, its list entry, `KernelRoutine` = the `ret 14h`, `NormalRoutine` = the gate, `NormalContext` = the request. link it into the threads kernel apc list at +0x34, set +0x49 (`KernelApcPending`) to 1 and set bit 2 in the software interrupt word.
6. `go`. the kernel runs the apc, the gate calls your function and bumps the counter.
7. `stop`, check the counter is 1 and the apc is gone, put the padding back, zero the request and `go`.

### stdcall, thiscall, fastcall and registers

that gate only does cdecl. for everything else +0x00 points at a second gate in 9 to 11 more padding runs. it saves esp, copies the 6 args again, loads eax ecx edx esi edi from +0x2C, calls the real target at +0x28 and then puts esp back, so it doesnt matter who pops the args. it also keeps edx at +0x68 and pops a float result off the x87 stack into +0x70. its bytes are `THUNK` in `og360/og/xefu/kernel_apc.py`.

### ebx, ebp and xmm registers

those two gates live in tiny kernel gaps and use ebx and ebp to find their way back after the call, so those cant carry args, and theres no room for xmm. for calls that need them OG360 builds a bigger third gate:

- it goes in the games own padding between functions: int3 or nop runs right after a `ret` or `jmp`, ending on a 16 byte boundary, at least 14 bytes each, chained with jumps. the game never runs those bytes so xefu converts the gate fresh the first time. its found and built for whatever game is running, so nothing gets ported per game.
- gate 1 calls it through the requests first word, same as the register gate.
- it keeps the stack pointer and the request in a 64 byte mailbox right after the request (that whole 256 bytes has to read all zeros first, so its nothing the game uses), which frees every register for args.
- it does `fxsave` of the games x87/sse state, `fninit` plus the games control word, loads xmm0-3 (single floats) from the mailbox, loads eax ecx edx esi edi from the request and ebx ebp from the mailbox, calls the target, keeps edx, xmm0 and any st0 float, then `fxrstor` and returns to gate 1.
- its never written back. if a gap ever held code that ran, xefu keeps that old version, so a new copy only ever goes in gaps that are still pure padding.

### calling between frames

calling right away runs your function in the middle of whatever that thread was doing. its safer to run it between frames:
- find the main thread. the games threads hang off its process (thread +0x44) at +0x8, linked through thread +0x104. the main one has a stack size (+0x1C minus +0x20) equal to the xbes `SizeOfStackCommit` at 0x10130.
- stop until its blocked in a kernel wait (state 5, wait irql 0) on events, mutants, threads or timers, and nothing else in the kernel is half done. not on a lock though. a critical section only records its owner after the wait, so a call that takes the same lock (malloc does) hangs the game.
- queue the apc to that thread with +0x49 set but dont set the interrupt bit. the kernel runs it as the thread wakes up, before the game gets control back.
- if it doesnt wake up in 3 seconds pull the apc back out and call right away instead.

some games need more. mafia waits inside directsound which holds its own lock, so OG360 only calls when its main thread is in its frame limiter sleep. halo 2s main thread never waits at all, so for one call OG360 points a pointer the game calls every frame (the cloth widget update at 0x4674F0) at a small hook in int3 gaps at the end of .text and puts it back after.

## other xefu versions

all 8 were done the same way:
1. each xefuN.xex has its x86 kernel stored inside at 0x80010000, already laid out like in memory. decompress the xex and cut it out from the kernels `MZ`. no game needed.
2. match the spots you know from a working xefu into it, with absolute addresses and jump offsets wildcarded. that gets you the kpcr, the software interrupt word, the `ret 14h` and the kernel code to hash.
3. pick padding nothing points to and check it disassembles back right.
4. before the first write on the console check it all live: hashes of the kernel code you rely on, every padding run still untouched with the same bytes in front of it, and kpcr +0x1C pointing at the kpcr itself.
5. for the ram pointer, each xefus ram setup stores the start and end in a global right before the nv2a pci setup. if you dont have it yet, scan 0xC0000000 to 0xE0000000 in 16 MB steps with the `MZ` and page directory check from above.

all hex:

| xefu | pe timestamp | entry | ram pointer | kpcr | sw interrupt | ret 14h |
|---|---|---|---|---|---|---|
| xefu | 437ACD68 | B4328 | 8217200C | 8002B43C | 8002BBC4 | 8001C33D |
| xefu1_1 | 4537DDE5 | B4570 | 8217200C | 8002B43C | 8002BBC4 | 8001C33D |
| xefu2 | 43E94E5E | B51A0 | 8217298C | 8002B4DC | 8002BC64 | 8001C3AD |
| xefu3 | 442C87BD | AF3F8 | 82197E8C | 8002B49C | 8002BC24 | 8001C3AD |
| xefu5 | 44F633D3 | AF058 | 821A818C | 8002B4BC | 8002BC44 | 8001C3AD |
| xefu6 | 457DD568 | C37D0 | 821B900C | 8002B4FC | 8002BC84 | 8001C3BD |
| xefu7 | 474B3577 | C3DA0 | 821DE210 | 8002B56C | 8002BCE4 | 8001C41D |
| xefu7b | 474B3577 | C1C48 | 821CE210 | 8002B56C | 8002BCE4 | 8001C41D |

## xefu notes

- xefu turns the x86 into ppc and keeps it forever, and it does that early, right as the game loads. so writing over the games code does nothing, even code the game hasnt run yet. if you change your gate bytes restart the game first, and always write the same bytes to the same spot.
- new code does run if its somewhere that never had code, like int3 padding, and you get there through a pointer read at runtime, like the apc or a function pointer in data. thats how to change what the game does while its running.
- swapping a return address on the stack doesnt work, xefu checks returns against its own copy.
- xefu only runs code in the ranges it registered (the games xbe sections and the kernels code) so your target has to be in there.
- max 6 stack args plus eax ecx edx esi edi ebx ebp and xmm0-3 as single floats. no wider xmm values, and only xmm0 comes back.
- each call stops the game for a second or two so you cant call stuff every frame.
- xbdm answers way faster while the console is stopped, so do your checks then.

## credits

- [XeCrippy](https://github.com/XeCrippy) for [py-xbdm](https://github.com/XeCrippy/py-xbdm). every request OG360 sends to the console goes through it.
- [nxdk](https://github.com/XboxDev/nxdk/blob/master/lib/xboxkrnl/xboxkrnl.h) for the kernel struct layouts and [xemu](https://github.com/xemu-project/xemu) for the xbe header.
- auroras nova plugin for showing the network fix could be done.
