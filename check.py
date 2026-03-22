import json, urllib.request

with open('run.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

run_id = data['workflow_runs'][0]['id']
print('Latest Run ID:', run_id)

url = 'https://api.github.com/repos/prideicker/FluentYTDL/actions/runs/' + str(run_id) + '/jobs'
req = urllib.request.Request(url)
with urllib.request.urlopen(req) as res:
    jobs = json.loads(res.read().decode())['jobs']

for job in jobs:
    print('Job:', job['name'], job['status'], job['conclusion'])
    ann_url = 'https://api.github.com/repos/prideicker/FluentYTDL/check_runs/' + str(job['id']) + '/annotations'
    ann_req = urllib.request.Request(ann_url)
    with urllib.request.urlopen(ann_req) as ann_res:
        anns = json.loads(ann_res.read().decode())
        for ann in anns:
            print('  Annotation:', ann['message'])
