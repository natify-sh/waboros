# CoreMark state machine (core_state_transition, pyc function f11) rewritten by hand: the br_table
# dispatch goes straight to the case code with no `_br` ladder. +11 % on all of CoreMark by itself;
# the `sink` rule of bropt.py produces this shape automatically.
def f11(p0, p1):
    l6 = 0
    l2 = U32(M,p0)[0]
    l3 = M[l2]
    if not l3:
        P32(M,p0,l2)
        return 0
    l2 = (l2+1)
    if l2 > 0xFFFFFFFF: l2 -= 0x100000000
    l4 = 0
    _br = 0
    while True:
        l5 = (l3&255)
        if (l5==44):
            _br = 4
            break
        if l4 == 0:
            if not (((l3-58)&255)<=245):
                l4 = 4
            else:
                l4 = 2
                t1 = (_w if (_w:=(l5-43)) >= 0 else _w+0x100000000)
                if t1 == 3:
                    l4 = 5
                elif t1 == 1 or t1 >= 4:
                    l4 = 1
                    P32(M,p1+4,(_w if (_w:=(U32(M,p1+4)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
            P32(M,p1,(_w if (_w:=(U32(M,p1)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
        elif l4 == 2:
            if not (((l3-58)&255)<246):
                P32(M,p1+8,(_w if (_w:=(U32(M,p1+8)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
                l4 = 4
            else:
                l6 = 1
                P32(M,p1+8,(_w if (_w:=(U32(M,p1+8)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
                if (l5!=46):
                    _br = 3
                    break
                l4 = 5
        elif l4 == 4:
            if not (l5!=46):
                P32(M,p1+16,(_w if (_w:=(U32(M,p1+16)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
                l4 = 5
            elif not (((l3-58)&255)<=245):
                l4 = 4
            else:
                P32(M,p1+16,(_w if (_w:=(U32(M,p1+16)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
                P32(M,p0,l2)
                return 1
        elif l4 == 5:
            if not ((l3&223)!=69):
                P32(M,p1+20,(_w if (_w:=(U32(M,p1+20)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
                l4 = 3
            elif not (((l3-58)&255)<=245):
                l4 = 5
            else:
                P32(M,p1+20,(_w if (_w:=(U32(M,p1+20)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
                P32(M,p0,l2)
                return 1
        elif l4 == 3:
            l6 = 1
            P32(M,p1+12,(_w if (_w:=(U32(M,p1+12)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
            l4 = 6
            t2 = (_w if (_w:=(l5-43)) >= 0 else _w+0x100000000)
            if not (t2 in (0, 2)):
                _br = 3
                break
        elif l4 == 6:
            l6 = 1
            P32(M,p1+24,(_w if (_w:=(U32(M,p1+24)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
            if (((l3-58)&255)>=246):
                l4 = 7
            else:
                _br = 3
                break
        elif l4 == 7:
            if (((l3-58)&255)<=245):
                _br = 2
                break
            l4 = 7
        l5 = (l2+1)
        if l5 > 0xFFFFFFFF: l5 -= 0x100000000
        l3 = M[l2]
        if not (l3==0):
            l2 = l5
            if (l4!=1):
                continue
        break
    if not _br:
        l2 = (l5-1)
        if l2 < 0: l2 += 0x100000000
    if _br == 4:
        _br = 0
    if not _br:
        l6 = l4
    if _br == 3:
        _br = 0
    if not _br:
        P32(M,p0,l2)
        return l6
    P32(M,p1+4,(_w if (_w:=(U32(M,p1+4)[0]+1)) <= 0xFFFFFFFF else _w-0x100000000))
    P32(M,p0,l2)
    return 1
