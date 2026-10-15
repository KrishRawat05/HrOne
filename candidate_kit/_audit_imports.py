import ast

with open('app/main.py') as f:
    tree = ast.parse(f.read())

imports = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            imports.add(alias.name.split('.')[0])
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            imports.add(node.module.split('.')[0])

stdlib = {
    'os', 're', 'datetime', 'contextlib', 'decimal', 'typing',
    'threading', 'time', 'uuid', 'pathlib', 'sys', 'random',
    'collections', 'functools', 'itertools', 'json', 'math',
    'string', 'abc', 'enum', 'dataclasses', 'copy', 'io',
}
third_party = imports - stdlib - {'__future__', ''}
print('Third-party imports in main.py:', sorted(third_party))
print()
print('=== requirements.txt ===')
print(open('requirements.txt').read())
