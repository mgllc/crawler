"""Versioned conservative extraction; raw bytes remain the primary evidence."""
import json
import re
from html.parser import HTMLParser

EXTRACTOR_VERSION = 'engineering-static-v1'
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
        'param', 'source', 'track', 'wbr'}
EXCLUDE = {'nav', 'footer', 'aside', 'script', 'style', 'noscript', 'template', 'svg', 'head'}
BLOCK = {'p', 'div', 'section', 'article', 'main', 'li', 'tr', 'h1', 'h2', 'h3',
         'h4', 'h5', 'h6', 'br', 'hr', 'dt', 'dd', 'blockquote'}


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate-json-key')
        result[key] = value
    return result


def strict_json(text):
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite-json')))


class ContentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.buffers = {'all': [], 'main': [], 'article': []}
        self.pre = False

    def emit(self, text, exact=False):
        if any(excluded for _, excluded in self.stack):
            return
        tags = [tag for tag, _ in self.stack]
        for key in self.buffers:
            if key == 'all' or key in tags:
                self.buffers[key].append((text, exact))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        excluded = (tag in EXCLUDE or 'hidden' in attrs
                    or attrs.get('aria-hidden', '').lower() == 'true')
        if tag not in VOID:
            self.stack.append((tag, excluded))
        if tag in BLOCK:
            self.emit('\n')
        if tag == 'pre':
            self.pre = True
            self.emit('\n[code]\n', True)
        if tag == 'a' and attrs.get('href'):
            self.emit(' [link:' + attrs['href'] + '] ')
        if tag == 'img' and attrs.get('alt'):
            self.emit(' [image:' + attrs['alt'] + '] ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.emit('\n[/code]\n', True)
            self.pre = False
        if tag in BLOCK:
            self.emit('\n')
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.emit(data, self.pre)

    def text(self):
        tokens = self.buffers['main'] or self.buffers['article'] or self.buffers['all']
        output, normal = [], []
        def flush():
            text = ''.join(normal)
            output.append('\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip()))
            normal.clear()
        for text, exact in tokens:
            if exact:
                flush()
                output.append(text)
            else:
                normal.append(text)
        flush()
        return ''.join(output).strip()


def meaningful_text(body, content_type):
    media = content_type.split(';')[0].strip().lower()
    charset = re.search(r'charset\s*=\s*["\']?([^;"\'\s]+)', content_type, re.IGNORECASE)
    if charset and charset.group(1).lower() not in ('utf-8', 'utf8', 'us-ascii'):
        raise ValueError('unsupported-charset')
    text = body.decode('utf-8-sig')
    if media == 'text/html':
        parser = ContentParser()
        parser.feed(text)
        parser.close()
        result = parser.text()
    elif media == 'text/plain':
        # Preserve indentation in code/config documentation.
        result = text.replace('\r\n', '\n').strip('\n')
    elif media == 'application/json':
        result = json.dumps(strict_json(text), sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    else:
        raise ValueError('unsupported-content-type')
    if not result.strip():
        raise ValueError('empty-extracted-content')
    return result
