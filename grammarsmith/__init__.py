"""grammarsmith — derive a tree-sitter grammar from a language's documentation + example code.

Point it at two structured input trees (Docs/, Examples/); it derives the construct inventory from the
docs, builds a model-adjudicated gold parse of the corpus as the single ground truth, searches candidate
grammars scored against that gold, and synthesises a tree-sitter grammar. Language-agnostic; zsh is the
reference instance (see languages/zsh/).
"""
__version__ = '0.0.1'
