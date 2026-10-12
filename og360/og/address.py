import re

TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_.:$@?]*|[0-9][0-9A-Fa-fxX]*|[\[\]()+\-*&,])")
FUNCTIONS = {"datum": 2, "block": 3}
CLOSING = {"[": "]", "(": ")"}


def parse(expr):
    tokens = TOKEN.findall(expr)
    if not tokens or "".join(tokens) != re.sub(r"\s+", "", expr):
        raise ValueError("dont get the address %r" % expr)
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def take():
        pos[0] += 1
        return tokens[pos[0] - 1]

    def factor():
        t = peek()
        if t is None:
            raise ValueError("the address %r isnt finished" % expr)
        take()
        if t in CLOSING:
            inner = total()
            if peek() != CLOSING[t]:
                raise ValueError("missing ] in %r" % expr)
            take()
            return ("read", inner) if t == "[" else inner
        if t[0].isdigit():
            try:
                return ("num", int(t, 16))
            except ValueError:
                raise ValueError("%r isnt a hex number" % t)
        if t[0].isalpha() or t[0] == "_":
            if peek() == "(" and t.lower() in FUNCTIONS:
                take()
                args = [total()]
                while peek() == ",":
                    take()
                    args.append(total())
                if peek() != ")":
                    raise ValueError("missing ) after %s(" % t)
                take()
                if len(args) != FUNCTIONS[t.lower()]:
                    raise ValueError("%s() takes %d values" % (t, FUNCTIONS[t.lower()]))
                return ("call", t.lower(), args)
            return ("word", t)
        raise ValueError("didnt expect %r in the address %r" % (t, expr))

    def term():
        v = factor()
        while peek() == "*":
            take()
            v = ("op", "*", v, factor())
        return v

    def sum_():
        v = term()
        while peek() in ("+", "-"):
            v = ("op", take(), v, term())
        return v

    def total():
        v = sum_()
        while peek() == "&":
            take()
            v = ("op", "&", v, sum_())
        return v

    tree = total()
    if peek() is not None:
        raise ValueError("didnt expect %r in the address %r" % (peek(), expr))
    return tree


def words(tree):
    kind = tree[0]
    if kind == "word":
        return [tree[1]]
    if kind == "read":
        return words(tree[1])
    if kind == "op":
        return words(tree[2]) + words(tree[3])
    if kind == "call":
        return [w for a in tree[2] for w in words(a)]
    return []


def is_hex(word):
    try:
        int(word, 16)
        return True
    except ValueError:
        return False


def evaluate(tree, word, read_u32, call):
    kind = tree[0]
    if kind == "num":
        return tree[1]
    if kind == "word":
        return word(tree[1])
    if kind == "read":
        return read_u32(evaluate(tree[1], word, read_u32, call) & 0xFFFFFFFF)
    if kind == "call":
        return call(tree[1], [evaluate(a, word, read_u32, call) & 0xFFFFFFFF for a in tree[2]])
    _, op, a, b = tree
    a, b = evaluate(a, word, read_u32, call), evaluate(b, word, read_u32, call)
    return a + b if op == "+" else a - b if op == "-" else a * b if op == "*" else a & b
