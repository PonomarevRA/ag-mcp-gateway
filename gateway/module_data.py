"""Bounded JSON operations used by data modules."""

import ast
import json
import math
import operator
import re
from hashlib import sha256

MAX_DATA = 262_144
_NUMERIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
            ast.Div: operator.truediv, ast.Mod: operator.mod}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class ModuleError(ValueError):
    """An operator-readable failure that contains no request or response data."""


def bounded_json(value, limit: int = MAX_DATA, *, node_limit: int = 12_000) -> bytes:
    """Reject deep, large or non-JSON data before using or returning it."""
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes + len(pending) > node_limit:
            raise ModuleError("Module JSON exceeds its structural limit")
        if isinstance(item, str) and len(item) > limit:
            raise ModuleError("Module JSON exceeds its byte limit")
        if isinstance(item, (dict, list)):
            if nodes + len(pending) + len(item) > node_limit:
                raise ModuleError("Module JSON exceeds its structural limit")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    try:
        # Iterative encoding avoids allocating an oversized intermediate string.
        chunks = bytearray()
        for piece in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(value):
            chunks.extend(piece.encode())
            if len(chunks) > limit:
                raise ModuleError("Module JSON exceeds its byte limit")
        return bytes(chunks)
    except ModuleError:
        raise
    except (ValueError, TypeError, UnicodeError) as error:
        raise ModuleError("Module data must be valid JSON") from error


def parse_json(text: str, *, allow_text: bool = False):
    """Parse and validate JSON, or keep plain text when explicitly permitted."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        if not allow_text:
            raise ModuleError("Module data must be valid JSON") from error
        value = text
    except (ValueError, RecursionError) as error:
        raise ModuleError("Module data must be valid JSON") from error
    bounded_json(value)
    return value


def pointer(value, path: str):
    """Resolve an RFC 6901 JSON Pointer and reject invalid array indexes."""
    if path == "":
        return value
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise ModuleError("Invalid JSON Pointer")
    for raw in path[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]{0,5}", key) and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ModuleError("JSON Pointer does not exist")
    return value


def object_required(value) -> dict:
    """Require an object for MCP arguments or structured output."""
    if not isinstance(value, dict):
        raise ModuleError("This operation requires a JSON object")
    return value


def array_required(value) -> list:
    """Require an array for filtering or aggregation."""
    if not isinstance(value, list):
        raise ModuleError("This operation requires a JSON array")
    return value


def same_scalar(left, right) -> bool:
    """Compare JSON scalar values without conflating booleans with numbers."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    return type(left) is type(right) and left == right



def json_signature(value) -> bytes:
    """Canonicalize JSON for revision checks without conflating true and 1."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return sha256(encoded).digest()


def contains(value, pattern, budget: list[int]) -> bool:
    """Test recursive object subsets and unordered array membership with a work limit."""
    budget[0] -= 1
    if budget[0] < 0:
        raise ModuleError("JSON containment exceeds its work limit")
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(
            key in value and contains(value[key], expected, budget) for key, expected in pattern.items()
        )
    if isinstance(pattern, list):
        return isinstance(value, list) and all(
            any(contains(item, expected, budget) for item in value) for expected in pattern
        )
    if isinstance(value, (dict, list)):
        return False
    return same_scalar(value, pattern)


def difference(left, right) -> list[dict]:
    """Report changed paths and kinds, leaving values in the original JSON."""
    changes = []

    def compare(before, after, path: str):
        """Recurse through common children and identify additions and removals."""
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key in before and key in after:
                    compare(before[key], after[key], child)
                else:
                    add(child, "added" if key in after else "removed")
        elif isinstance(before, list) and isinstance(after, list):
            for index in range(max(len(before), len(after))):
                child = f"{path}/{index}"
                if index < len(before) and index < len(after):
                    compare(before[index], after[index], child)
                else:
                    add(child, "added" if index < len(after) else "removed")
        elif not same_scalar(before, after):
            add(path, "changed")

    def add(path: str, change: str):
        """Stop before a pathological difference can exhaust memory."""
        if len(changes) >= 512 or len(path) > 1024:
            raise ModuleError("JSON comparison exceeds its change limit")
        changes.append({"path": path, "change": change})

    compare(left, right, "")
    return changes


def number(value) -> float:
    """Accept only finite JSON numbers in the supported formula range."""
    if type(value) not in (int, float) or abs(value) > 1e100 or not math.isfinite(value):
        raise ModuleError("Formula requires finite numbers within range")
    return float(value)


def formula_tree(expression: str, names) -> ast.Expression:
    """Parse simple arithmetic and reject anything outside its grammar."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ModuleError("Invalid arithmetic formula") from error
    for index, node in enumerate(ast.walk(tree)):
        if index > 64:
            raise ModuleError("Arithmetic formula is too complex")
        if isinstance(node, ast.Name) and node.id not in names:
            raise ModuleError("Arithmetic formula uses an unknown variable")
        if isinstance(node, ast.Constant):
            number(node.value)
        if not isinstance(node, (ast.Expression, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp, ast.Constant,
                                 *_NUMERIC, *_UNARY)):
            raise ModuleError("Unsupported arithmetic operation")
    return tree


def formula(expression: str, variables: dict[str, float]) -> float:
    """Evaluate the explicitly supported AST nodes without evaluating Python code."""
    tree = formula_tree(expression, variables)

    def evaluate(node) -> float:
        """Interpret one validated node and bound intermediate numeric values."""
        if isinstance(node, ast.Constant):
            return number(node.value)
        if isinstance(node, ast.Name):
            return variables[node.id]
        if isinstance(node, ast.UnaryOp):
            return number(_UNARY[type(node.op)](evaluate(node.operand)))
        left = evaluate(node.left)
        right = evaluate(node.right)
        if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
            raise ModuleError("Division by zero")
        return number(_NUMERIC[type(node.op)](left, right))

    return evaluate(tree.body)
