def latest(events,kind,key):
    out={}
    for e in events:
        if e['kind']==kind: out[e[key]]=e
    return out

def d(result,reason,*ids): return {'result':result,'reason':reason,'evidence':[x for x in ids if x]}

def calculate(events):
    plans=latest(events,'plan','job_id')
    assets=latest(events,'asset','job_id')
    works=latest(events,'work','job_id')
    out={}; states=[]
    for job_id,p in plans.items():
        a=assets.get(job_id); w=works.get(job_id)
        mass=p.get('measured_kg',0); radius=p.get('radius_m',0)
        moment=mass*radius
        center=p.get('center_ok',False)
        route=p.get('route_clear',False)
        signed=p.get('signed',False)
        out['吊物重量版本核定']=d('有效' if mass>0 else '待复测','有效实测重量参与额定力矩',p['id'])
        out['吊点重心方案签认']=d('签认' if center and signed else '撤回','吊点覆盖重心包络且签认有效',p['id'])
        out['回转路径禁区核验']=d('可通行' if route else '重画','路径不得经过临时禁区',p['id'])
        out['方案签认撤回']=d('有效' if signed else '撤回','撤回方案不得开启新工单',p['id'])
        states.append(('吊装方案账',job_id,'签认' if center and route and signed else '待修订',p.get('version',1)))
        if a:
            crane=a.get('inspection_valid') and moment<=a.get('rated_moment',0)
            rigging=a.get('rigging_valid') and a.get('rigging_capacity_kg',0)>=mass
            staff=a.get('staff_valid') and not a.get('staff_conflict')
            out['塔机额定载荷复核']=d('合格' if crane else '超限或检验失效','半径乘重量与额定力矩比较',a['id'],p['id'])
            out['索具组合适配']=d('合格' if rigging else '失配','全部索具检验与载荷组合校核',a['id'],p['id'])
            out['指挥资质排班确认']=d('覆盖' if staff else '缺口','人员证书与排班覆盖作业窗口',a['id'])
            out['设备保留冲突']=d('冲突' if a.get('reserved_elsewhere') else '无冲突','同塔机时窗不得重复保留',a['id'])
            states.append(('设备与人员资格账',job_id,'可用' if crane and rigging and staff and not a.get('reserved_elsewhere') else '冻结',a.get('version',1)))
        if w:
            windy=w.get('wind_mps',999)>w.get('wind_limit_mps',0) or not w.get('wind_calibrated',False)
            closed=w.get('hooked') and w.get('lifted') and w.get('landed') and w.get('witnessed')
            ready=bool(a) and crane and rigging and staff and not a.get('reserved_elsewhere') and center and route and signed and not windy
            out['现场风速停复工']=d('放行' if ready else '停工','风速校准、方案和设备共同决定复工',w['id'],p['id'])
            out['起落钩闭环核验']=d('完工' if ready and closed else '悬停或待核','挂钩、离地、落钩和独立签收完整',w['id'],a['id'] if a else p['id'])
            out['悬吊物应急落地']=d('应急落地' if windy and w.get('lifted') and not w.get('landed') else '无需','在途停工不可仅更新风速结论',w['id'])
            out['监理独立见证']=d('见证' if w.get('witnessed') else '待见证','落钩需要独立监理签认',w['id'])
            states.append(('现场许可与作业账',job_id,'完工' if ready and closed else '停工或待核',w.get('version',1)))
    return out,states

RECORD_SPECS={'plan': ('吊装方案账', 'job_id', [('吊物测量', ['measured_kg', 'radius_m']), ('吊点方案', ['center_ok', 'route_clear', 'signed'])]), 'asset': ('设备与人员资格账', 'job_id', [('塔机索具检验', ['inspection_valid', 'rated_moment', 'rigging_valid', 'rigging_capacity_kg']), ('人员排班', ['staff_valid', 'staff_conflict', 'reserved_elsewhere'])]), 'work': ('现场许可与作业账', 'job_id', [('风速观测', ['wind_mps', 'wind_limit_mps', 'wind_calibrated']), ('起落钩事件', ['hooked', 'lifted', 'landed', 'witnessed'])])}

def business_records(event):
    subsystem,key,specs=RECORD_SPECS[event['kind']]
    return [(subsystem,name,str(event[key]),{field:event.get(field) for field in fields}) for name,fields in specs]
