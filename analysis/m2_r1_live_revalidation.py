import json,sys,time,importlib.util,collections
sys.path.insert(0,"/app/scripts")
from hermes_trader.agents import executor as ex
from hermes_trader.agents.config_store import read_agent_config
from hermes_trader.indicators import triggers as T
import replay_gate as rg
cfg=read_agent_config()
cfg["runner_entry_gate"]["mover_bypass_veto"]={"shadow_mode":True}
calls=[]
ex._record_risk_tuning_shadow=lambda **k:calls.append(k.get("rule"))
th=th=rg.trigger_thresholds_params(config=cfg); w=rg.trigger_weights_params(config=cfg)
mover_cfg=(cfg.get("runner_entry_gate") or {}).get("daily_mover") or {}
mover_cfg.setdefault("enabled",True)
data=rg.Data(source="binance")
# coins that had admitted movers in replay
rows=[json.loads(l) for l in open("/data/decision_shadow_replay_bn90d.jsonl")]
byc=collections.Counter(r["coin"] for r in rows if r.get("admitted") and r.get("dMover") and not r.get("fresh_impulse"))
coins=[c for c,_ in byc.most_common(12)]
print("coins",coins)
end=int(time.time()*1000);start=end-40*86400000
tot_admit=0;tot_mover_admit=0;tot_r1=0
for coin in coins:
    r2=rg.replay_coin(data,coin,start,end,step_bars=12,thresholds=rg.trigger_thresholds_params(config=cfg),weights=w,config=cfg,conf_assumed=0.9)
    for r in r2:
        if r["admitted"]:
            tot_admit+=1
            if r.get("dMover") and not r.get("fresh_impulse"):tot_mover_admit+=1
    tot_r1=calls.count("mover_bypass_veto")
print("admitted",tot_admit,"mover_admitted",tot_mover_admit,"R1_shadow_records(total)",tot_r1)