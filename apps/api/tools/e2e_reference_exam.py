"""Real isolated upload/parse/preflight/generation roundtrip; model endpoint remains mocked."""
import io
import json
import time
import zipfile
import requests
import e2e_review_realapi as scenario


def check_exam():
    text = '历年卷第一题：计算临界角。第二题：计算薄透镜像距，给出逐步推导和评分点。'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('word/document.xml', '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+text+'</w:t></w:r></w:p></w:body></w:document>')
    title = '历年卷参考：2025期末卷.docx'
    r = requests.post(scenario.BASE+'/imports', data={'payload':json.dumps({'course_id':'crs_3f8a21','kind':'document','title':title})},
                      files={'files':('2025期末卷.docx',buffer.getvalue(),'application/vnd.openxmlformats-officedocument.wordprocessingml.document')},timeout=15)
    assert r.status_code == 202, r.text
    job = scenario.poll_job(r.json()['job']['id'])
    assert job['status'] == 'succeeded', job.get('errors')
    resource_id = next(ref['id'] for ref in job['result_refs'] if ref['type']=='resource')
    for _ in range(100):
        resource = requests.get(scenario.BASE+'/resources/'+resource_id,timeout=5).json()
        if resource['parse_status'] in ('ready','failed','skipped'):break
        time.sleep(.2)
    assert resource['parse_status'] == 'ready', resource
    provider = requests.get(scenario.BASE+'/ai/providers',timeout=5).json()['items'][0]
    body = {'course_id':'crs_3f8a21','mode':'ai','provider_config_id':provider['id'],'title':'历年卷参考联调卷',
            'materials':{'resources':[{'resource_id':'res_01a7'},{'resource_id':resource_id}],'assignments':[{'assignment_id':'asg_a102','include_attachments':True}]},
            'notes':'历年卷只参考题型、难度和分值，仍以本课程PPT和作业为主。\n请保留PPT中的定义和公式依据。',
            'constraints':{'question_types':[{'type':'single_choice','count':4},{'type':'short_answer','count':2}]}}
    pre = requests.post(scenario.BASE+'/review-papers/preflight',json=body,timeout=10)
    assert pre.status_code == 200,pre.text
    assert text in pre.text and title in pre.text
    result = requests.post(scenario.BASE+'/review-papers/generate',json=body,headers={'Idempotency-Key':'reference-exam-e2e','If-Match':pre.json()['fingerprint']},timeout=10)
    assert result.status_code == 202,result.text
    generated = scenario.poll_job(result.json()['job']['id'])
    assert generated['status'] == 'succeeded',generated.get('errors')
    messages = scenario.CAPTURED[-1]['payload']['messages']
    assert text in messages[1]['content'] and body['notes'] in messages[1]['content']
    assert '优先使用本课程课件/PPT与作业正文' in messages[0]['content']
    assert '不默认照搬原题' in messages[0]['content']
    scenario.check('真实上传历年卷→解析正文→预览→生成请求含正文及主次规则',True)


if __name__=='__main__':
    original=scenario.check
    def wrapped(name,cond,detail=''):
        original(name,cond,detail)
        if name=='题库能力端点 200' and cond:check_exam()
    scenario.check=wrapped
    raise SystemExit(scenario.main())
