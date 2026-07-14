# LineGuard 演示视频分镜脚本(≤5 分钟)

> 录屏工具:QuickTime(⌘⇧5)或 Loom。分辨率 1080p+,终端字号调大(⌘+)。
> 提前准备:两个终端窗口 + 一个浏览器窗口(Streamlit),排练一遍再录。

## 录制前准备(一次性)

    cd ~/lineguard && source .venv/bin/activate
    python -m lineguard.backtest --data data/        # 若已跑过可跳过
    python -m lineguard.analysis --data data/        # 生成衰减曲线
    rm -f data/decisions.jsonl                       # 清空决策日志,录制干净

## 分镜

### 0:00–0:35 问题(对着幻灯片或 README 讲)
台词要点:
- "这个赛道大家都在做信号检测。但 29 场世界杯、288 万条真实赔率记录告诉我们:检测不是难题,**行动才是**。"
- 亮出核心数字(与 results/RESULTS.md 一致):"信号出现 60 秒内,按 2% 保守滑点计,可锁定盈亏平均已跌至 −12.5/100。尖峰是易逝的,人手速锁不住,自主 agent 可以。"
- "LineGuard 不是又一个探测器,是一个自主风险台:检测→闭式对冲→链上留痕。"

### 0:35–2:30 主演示:回放阿根廷 vs 瑞士(1/4决赛)
终端 1:
    python -m lineguard.agent --replay "data/hist_odds_18222446_*.jsonl" "data/hist_scores_18222446_*.jsonl" --speed 120
镜头语言:
- OPEN 出现时暂停旁白:"开赛,agent 自动建仓,回放的是真实 TxLINE 去水共识价"
- LOCK 出现时:"尖峰出现的一瞬间,agent 用闭式解 F_lock = S(a·q−1) 完成对冲——
  三种终局盈亏全部锁定,数字就在屏幕上"
- 指出每行结尾的 ⚓ 交易签名:"每个决策实时锚定在 Solana devnet"

### 2:30–3:10 名场面:Guard 拒绝脏数据
    python -m lineguard.agent --replay "data/hist_odds_18222446_*.jsonl" --speed 300 --inject-stale
- REJECT_G1 出现时:"我们向流中注入了一条 2.5 小时前的过期报价。TxLINE 把数据锚上链,
  LineGuard 在消费端验证锚——过期、去水失衡、编码不一致,任何一项不过,决策不执行,
  拒绝本身也上链留痕。"

### 3:10–4:10 仪表盘 + 链上验证
    streamlit run lineguard/dashboard.py
- 指标卡:决策数 / 锁定盈亏合计 / Guard 拦截数 / 链上锚定数
- 点开一条 LOCK 决策 → 点 "view tx on Solana explorer" → 浏览器展示真实 devnet 交易
- 滚动到衰减曲线图:"这就是那条衰减曲线,328 个信号的平均(与 results/RESULTS.md 完全一致)——我们锁在 t=0"

### 4:10–5:00 收尾
- 架构一页图(README 里的 ascii 图截屏即可):ingest → guard → signal(含比分归因)→ hedge → anchor
- "无 LLM 决策路径、纯函数 + 18 项单元测试、断线重连、回放模式保证赛后可评审"
- 报出仓库链接 + 一句话:"Detection is table stakes. LineGuard acts."

## 备用镜头(强烈建议录)
- 明天法西之战 LIVE 模式跑 60 秒真实直播数据:`python -m lineguard.agent --live`
  (哪怕只露 10 秒 "LIVE" 字样,评委对 live-data 的疑虑瞬间消除)
