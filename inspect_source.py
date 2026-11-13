"""Fixed no-execution Python syntax inspection capability."""
import ast
from pathlib import Path
import sys
path = Path(sys.argv[1])
ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
print('Python AST parse passed: ' + path.name)
