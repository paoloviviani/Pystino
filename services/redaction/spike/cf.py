"""Synthetic Italian fiscal codes with a valid check character (fake names only)."""
ODD = {**dict(zip("0123456789", [1,0,5,7,9,13,15,17,19,21])), **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [1,0,5,7,9,13,15,17,19,21,2,4,18,20,11,3,6,8,12,14,16,10,22,25,24,23]))}
def cf(base15: str) -> str:
    s = 0
    for i, ch in enumerate(base15):
        if i % 2 == 0:
            s += ODD[ch]
        else:
            s += int(ch) if ch.isdigit() else ord(ch) - 65
    return base15 + chr(65 + s % 26)
if __name__ == "__main__":
    for b in ["RSSMRA80A01H501", "BNCLRA75M41F205", "DVRGPP62D15L219", "FRRMRT90C60A944", "CSTSFN55H10G273", "PLLNNA88S45D612"]:
        print(cf(b))
