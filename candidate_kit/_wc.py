import re
text = open('DECISIONS.md').read()
body = re.sub(r'^#.*$', '', text, flags=re.MULTILINE)
words = len(body.split())
print(f'Word count (body only, no headings): {words}')
print(f'Total word count (including headings): {len(text.split())}')
