from dataclasses import dataclass

from dataclass_native import DiscriminatedUnion, native


@native
@dataclass(frozen=True)
class Node:
    value: int
    next: "Node | None" = None
    children: "tuple[Node, ...]" = ()


@native
@dataclass(frozen=True)
class Expr:
    op: str
    left: "DiscriminatedUnion[Expr, Leaf]"
    right: "DiscriminatedUnion[Expr, Leaf] | None" = None


@native
@dataclass(frozen=True)
class Leaf:
    value: float
    parent: "Wrapper | None" = None


@native
@dataclass(frozen=True)
class Wrapper:
    expr: Expr
