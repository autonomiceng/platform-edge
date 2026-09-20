"""HTTP acceptance against smoke-owned Edge and stubs; invoked only by smoke.sh."""
import json
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

base = sys.argv[1]
absent = '--absent-observability' in sys.argv
edge_present = '--edge-status' in sys.argv
opener = build_opener(ProxyHandler({}))
checks = 0


def request(stack, method='GET', mode='', host='localhost'):
    global checks
    headers = {'Host': host, 'Authorization': 'Bearer smoke-private',
               'Cookie': 'session=smoke-private', 'X-Smoke-Status': mode}
    headers.update({'Range': 'bytes=0-2', 'If-Range': '"smoke-range"',
                    'If-Match': '"not-current"', 'If-Unmodified-Since': 'Thu, 01 Jan 1970 00:00:00 GMT',
                    'If-None-Match': '*',
                    'If-Modified-Since': 'Wed, 31 Dec 2099 23:59:59 GMT'})
    path = '/status.json' if stack == 'direct-edge' else f'/stack-status/{stack}'
    req = Request(f'{base}{path}', headers=headers, method=method)
    try:
        response = opener.open(req, timeout=6)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(65537)
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers.get_content_type() == 'application/json'
        if response.status == 405:
            assert response.headers['Allow'] == 'GET, HEAD'
        checks += 1
        return response.status, body


for host in ('localhost', 'private.test.ts.net'):
    for stack, alias in [('gateway', 'lg-gateway'), ('backplane', 'bp-gateway'),
                         ('observability', 'ob-gateway')]:
        if absent and stack == 'observability':
            assert request(stack, host=host) == (502, b'')
            continue
        status, body = request(stack, host=host)
        assert status == 200
        result = json.loads(body)
        assert result['producer'] == alias
        assert result['host'] == ('backplane.localhost' if stack == 'backplane' else 'localhost')
        assert result['scheme'] == 'http'
        assert request(stack, method='HEAD', host=host) == (200, b'')
        assert request(stack, method='POST', host=host) == (405, b'')
        for mode, expected in [('missing', 404), ('failure', 503), ('html', 502)]:
            assert request(stack, mode=mode, host=host) == (expected, b'')
    for edge in ('edge', 'direct-edge'):
        status, body = request(edge, host=host)
        if edge_present:
            assert status == 200
            assert json.loads(body)['stack'] == 'edge'
            assert json.loads(body)['generatedAt'] == '2026-09-20T00:00:00Z'
        else:
            assert (status, body) == (404, b'')
        assert request(edge, method='HEAD', host=host) == (200 if edge_present else 404, b'')
        assert request(edge, method='POST', host=host) == (405, b'')
print(f'PASS: {checks} status proxy requests')
