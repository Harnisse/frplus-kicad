"""Specctra DSN / SES 用の最小限の S 式処理。"""
import re


def tokenize(s):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in '()':
            yield c
            i += 1
        elif c == '"':
            j = s.index('"', i + 1)
            yield ('q', s[i + 1:j])
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not s[j].isspace() and s[j] not in '()':
                j += 1
            yield ('a', s[i:j])
            i = j


def parse(s):
    """入れ子のリストにする。引用符の有無は捨てる (書き戻すときは q() で付け直す)。"""
    stack = [[]]
    for t in tokenize(s):
        if t == '(':
            stack.append([])
        elif t == ')':
            x = stack.pop()
            stack[-1].append(x)
        else:
            stack[-1].append(t[1])
    return stack[0][0]


def q(name):
    """括弧や空白を含む名前は引用符で囲む。"""
    return '"%s"' % name if name == '' or re.search(r'[\s()"]', name) else name


def num(v):
    """DSN の分解能 (0.1um) を落とさずに書く。%g は 6 桁に丸めるので端点がずれて接続が切れる。"""
    return ('%.1f' % v).rstrip('0').rstrip('.')


def block_end(s, start):
    """s[start] == '(' に対応する ')' の次の位置 (引用符内の括弧は無視)。

    DSN 冒頭の (parser (string_quote ")) は引用符が閉じないので、それより後ろのブロックにだけ使うこと。
    """
    depth, i, inq = 0, start, False
    while True:
        c = s[i]
        if c == '"':
            inq = not inq
        elif not inq:
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1


def child(node, name):
    return next(x for x in node if isinstance(x, list) and x and x[0] == name)
