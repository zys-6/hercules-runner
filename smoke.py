"""Real CLI integration check against the running target's read-only health API."""
import json
from pathlib import Path
import time
import urllib.request
import uuid

base = 'http://127.0.0.1:8096'
token = (Path(__file__).parent / 'data/service.token').read_text().strip()
headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
for expected, wanted in ((200, 'passed'), (201, 'failed')):
    body = {'schemaVersion': '1', 'requestId': str(uuid.uuid4()), 'targetUrl': 'http://127.0.0.1:8080',
            'timeoutSeconds': 180, 'cases': [{'caseId': expected, 'name': 'Read-only target health smoke',
            'precondition': 'No login required. Use API helper only, no browser. The health endpoint is public.',
            'steps': f'请求：GET /api/health\n期望状态码：{expected}\n断言：$.code=0',
            'expectedResult': f'The response HTTP status must be exactly {expected} and JSON code must equal 0. Do not change this expectation.'}]}
    request = urllib.request.Request(base + '/runs', data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        job = json.load(response)
    print('submitted', job['runId'], 'expected', wanted, flush=True)
    deadline = time.monotonic() + 240
    while job['state'] in ('queued', 'running'):
        if time.monotonic() > deadline: raise SystemExit('Client deadline exceeded')
        time.sleep(2)
        with urllib.request.urlopen(urllib.request.Request(base + '/runs/' + job['runId'], headers=headers), timeout=10) as response:
            job = json.load(response)
    print(json.dumps(job), flush=True)
    if job['state'] != wanted: raise SystemExit(f"Expected {wanted}, got {job['state']}")
