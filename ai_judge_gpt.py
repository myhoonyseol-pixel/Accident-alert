"""GPT comparison judge for Accident-alert. Claude's ai_judge.py remains untouched."""
import json, os, sys
from openai import OpenAI

MODEL=os.getenv("OPENAI_MODEL","gpt-5-mini")
SYSTEM_PROMPT='''너는 건설현장 사고속보 감시기의 독립 GPT 비교 판정기다.\n\n각 기사에 대해 decision은 ALERT 또는 NO_ALERT, type은 NEW/UPDATE/IRRELEVANT/DUP 중 하나로 판단한다.\n\nALERT: 국내 건설현장/사업장에서 발생한 사망, 중상·중태, 의식불명, 매몰·고립·실종, 심정지, 구조 진행 중인 중대사고. 건설현장의 방금 발생한 붕괴·폭발·타워크레인 전도 등 구조적 사고는 인명피해가 명확하지 않아도 속보 가치가 있으면 ALERT. 제조공장·물류창고·산업단지는 사망/중상/중대 화재·폭발 중심. 한국 건설·전력 관련 기업의 해외 공사현장 또는 한국인 근로자 사고도 포함.\n\nNO_ALERT: 단순 통계, 캠페인, 점검·감독 자체, 법원판결·행정처분·제도설명, 과거 사고의 원인/책임/대책 후속기사, 단순 경영뉴스, 교통사고·범죄·산불·자연재해·익수 등 무관 사건, 일반 주거/개인 사고, 경상, 과거 사고 재보도.\n\n기존 사고와 같은 사고면 DUP. 같은 사고라도 사망자 증가·피해 확대·구조상황 등 새로운 중대 사실이면 UPDATE.\n\n한국어 문자열 오탐 주의: 대전도시공사의 전도, 구미국가산단의 미국, 안전대책의 안전대, 경상매일신문의 경상 등을 단순 키워드로 사고 판단하지 않는다.\n\n기사 제목과 제공된 요약/본문, 시간, 장소를 종합해 판단한다. JSON 배열만 출력한다.'''
SCHEMA={"type":"array","items":{"type":"object","additionalProperties":False,"properties":{"index":{"type":"integer"},"decision":{"type":"string","enum":["ALERT","NO_ALERT"]},"type":{"type":"string","enum":["NEW","UPDATE","IRRELEVANT","DUP"]},"reasoning":{"type":"string"}},"required":["index","decision","type","reasoning"]}}

def _text(c,i):
    item=c[0] if isinstance(c,(list,tuple)) else c
    place=c[1] if isinstance(c,(list,tuple)) and len(c)>1 else ''
    hits=c[2] if isinstance(c,(list,tuple)) and len(c)>2 else []
    if isinstance(item,dict):
        return f'''[{i}]\n제목: {item.get("title","")}\n출처: {item.get("source",item.get("publisher",""))}\n발행시각: {item.get("published",item.get("pubDate",""))}\n장소/분류: {place}\n매칭어: {hits}\nURL: {item.get("url","")}\n요약/본문: {item.get("summary",item.get("description",""))}'''
    return f'[{i}]\n제목: {item}\n장소/분류: {place}\n매칭어: {hits}'

def judge_gpt(candidates,recent_events=None):
    if not candidates: return []
    try:
        key=os.getenv('OPENAI_API_KEY')
        if not key: raise RuntimeError('OPENAI_API_KEY가 설정되어 있지 않습니다.')
        client=OpenAI(api_key=key)
        body='\n\n'.join(_text(c,i) for i,c in enumerate(candidates))
        recent=''
        if recent_events: recent='\n최근 전달된 사고 목록(중복/업데이트 판단용):\n'+json.dumps(recent_events,ensure_ascii=False,default=str)[:12000]
        r=client.responses.create(model=MODEL,store=False,input=[{'role':'system','content':SYSTEM_PROMPT},{'role':'user','content':'다음 후보를 각각 독립적으로 판정해라.'+recent+'\n\n'+body}],text={'format':{'type':'json_schema','name':'accident_judgments','strict':True,'schema':SCHEMA}})
        data=json.loads(r.output_text)
        by={int(x['index']):x for x in data}
        out=[]
        for i,c in enumerate(candidates):
            item=c[0] if isinstance(c,(list,tuple)) else c; place=c[1] if isinstance(c,(list,tuple)) and len(c)>1 else ''; hits=c[2] if isinstance(c,(list,tuple)) and len(c)>2 else []; conf=c[3] if isinstance(c,(list,tuple)) and len(c)>3 else 0
            j=by[i]; typ=j['type']
            out.append((item,place,hits,conf,{'decision':j['decision'],'v':typ.lower(),'type':typ,'reason':j.get('reasoning',''),'model':MODEL}))
        return out
    except Exception as e:
        print(f'[gpt] 판정 실패: {e}',file=sys.stderr)
        out=[]
        for c in candidates:
            item=c[0] if isinstance(c,(list,tuple)) else c; place=c[1] if isinstance(c,(list,tuple)) and len(c)>1 else ''; hits=c[2] if isinstance(c,(list,tuple)) and len(c)>2 else []; conf=c[3] if isinstance(c,(list,tuple)) and len(c)>3 else 0
            out.append((item,place,hits,conf,{'decision':'ERROR','v':'error','type':'ERROR','reason':str(e),'model':MODEL}))
        return out
