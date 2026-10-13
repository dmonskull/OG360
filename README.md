# OG360

OG360 is for modding og xbox games while theyre running on the xbox 360 under xefu (the xbox 360s backwards compatibility emulator for original xbox games). it talks to a jtag/rgh console over xbdm so you can flip mods on and off live, change or lock values, search memory, call the games own functions and boot games straight from the hdd.

the memory tools work on any og xbox game. these ones also come with ready made mods and cheats:

- halo ce
- halo 2
- mafia
- true crime streets of la
- aggressive inline
- need for speed underground 2
- gauntlet dark legacy (just a few values for now)
- destroy all humans
- mortal kombat deception (not tested yet)
- dave mirra freestyle bmx 2 (unlocks, not tested yet)

more features are planned for the future.

## what you need

a jtag/rgh xbox 360 with xbdm, and jrpc2 added as a plugin in dashlaunch. without jrpc2 the memory stuff and mods still work, but OG360 cant keep the xbox online in games like halo 2 and screenshots only work in some games.

## python

made and tested on python 3.13 with tk 9.1. anything from 3.9 up with tkinter works, the launcher sets up numpy and pillow on its own.

- mac: `brew install python@3.13 python-tk@3.13`, or the installer from python.org (tk comes with it). tk older than 8.6.13 works but clicks can just focus the window first
- windows: python 3.13 from python.org
- linux: python3 with tkinter (`sudo apt install python3-tk python3-venv`)

## quick guide

**search** finds a value like health or ammo.
1. leave the type on any number if you dont know it. type the number you see in game and hit first scan
2. change it in game, type the new number and hit next scan. if you cant see the number use went up or went down instead
3. keep going till theres only a few left. double click one to see it in memory or hit add to mods

**changes** is for when you dont know the value at all.
1. pick an area and take a first snapshot
2. while nothing is happening hit ignore whats changing now. that hides timers and animations
3. do the thing in game and hit changed since first snapshot. everything that changed shows up with before and after

**memory** is the hex view. type an address and hit go, click a byte to see it as every type and write a new value on the right.

**mods** has the ready made stuff for your game. tick a switch to turn it on. values update live and lock keeps one stuck where you set it.

if you want to know how it works or do it yourself check out [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

## credits

[XeCrippy](https://github.com/XeCrippy) for [py-xbdm](https://github.com/XeCrippy/py-xbdm). everything OG360 sends to the console goes through it.
