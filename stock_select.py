import akshare as ak
import pandas as pd
import numpy as np
import requests
import time
import os
from datetime import datetime, timedelta

# 飞书推送
def send_feishu(content):
    webhook = os.environ.get('FEISHU_WEBHOOK')
    if not webhook:
        print("未找到飞书Webhook")
        return
    data = {
        "msg_type": "text",
        "content": {"text": content}
    }
    try:
        requests.post(webhook, json=data)
        print("推送完成")
    except Exception as e:
        print("推送失败", e)

# 判断交易日
def is_trade_day(date):
    try:
        trade_dates = ak.tool_trade_date_hist_sina()
        return date.strftime('%Y%m%d') in trade_dates['trade_date'].astype(str).tolist()
    except:
        return True  # 接口异常默认继续

# 获取初步股票池
def get_stock_pool():
    df = ak.stock_zh_a_spot_em()
    # 只留主板60、00开头
    df = df[df['代码'].str.startswith(('60', '00'))].copy()
    # 过滤ST、*ST、新股N
    df = df[~df['名称'].str.contains(r'ST|\*ST|N')].copy()
    # 未停牌、高开2-7%、价格<55元（初步过滤，减少计算量）
    df = df[df['今开'] > 0].copy()
    df = df[(df['涨跌幅'] >= 2) & (df['涨跌幅'] <= 7)].copy()
    df = df[df['最新价'] < 55].copy()
    return df.reset_index(drop=True)

# 计算单只股票技术指标
def calc_indicators(code):
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=120)).strftime('%Y%m%d')
    try:
        k = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
    except:
        return None
    if len(k) < 30:
        return None
    k = k.sort_values('日期').reset_index(drop=True)

    # 每日涨停判断（主板10%光头涨停）
    k['涨停'] = (k['收盘'] >= round(k['收盘'].shift(1)*1.1, 2)) & (k['最高'] == k['收盘'])
    
    # 昨连板数
    lb = 0
    for i in range(len(k)-2, -1, -1):
        if k['涨停'].iloc[i]:
            lb += 1
        else:
            break

    # 昨日/前日K线
    yest = k.iloc[-2]
    day2 = k.iloc[-3] if len(k)>=3 else yest
    day3 = k.iloc[-4] if len(k)>=4 else day2

    yest_yz = (yest['开盘']==yest['收盘']==yest['最高']==yest['最低'])
    yest_amp = (yest['最高']-yest['最低'])/day2['收盘']*100
    liangsuo = (yest['成交量'] < day2['成交量']*0.65) or (yest['成交量'] < day3['成交量']*0.75)
    diliang = yest['成交量'] <= k['成交量'].iloc[-5:-1].min() if len(k)>=5 else False
    juliang = yest['成交量'] >= day2['成交量']*4
    datie = (day2['收盘']/day3['收盘']) <= 0.93

    # 均线
    k['ma5'] = k['收盘'].rolling(5).mean()
    k['ma20'] = k['收盘'].rolling(20).mean()
    ma5 = k['ma5'].iloc[-2]
    ma20_20 = k['ma20'].iloc[-21] if len(k)>=21 else k['ma20'].iloc[0]
    jq_zf = (k['收盘'].iloc[-1] - ma20_20)/ma20_20*100

    return {
        'zuo_lb': lb,
        'yest_yz': yest_yz,
        'yest_amp': yest_amp,
        'liangsuo': liangsuo,
        'diliang': diliang,
        'juliang': juliang,
        'datie': datie,
        'ma5': ma5,
        'jq_zf': jq_zf,
        'yest_vol': yest['成交量'],
        'yest_close': yest['收盘']
    }

# 策略1：原始版轮回
def score_s1(row, ind):
    if ind['zuo_lb'] >= 5:
        return 0
    if row['最新价'] >= 30.95:
        return 0
    zjpx9 = row['最新价'] * ((row['涨跌幅']/100)**3) * 10000
    if zjpx9 <= 10:
        return 0
    return round((row['涨跌幅']**3)/10 + ind['zuo_lb'], 2)

# 策略2：轮回V2.4
def score_s2(row, ind):
    price = row['最新价']
    pct = row['涨跌幅']
    jj_amount = row['成交额'] * 10000  # 转元
    jj_vol = row['成交量']  # 手

    # 母池门槛
    if ind['zuo_lb'] >= 3:
        return 0
    blb = jj_vol * 100 / ind['yest_vol'] if ind['yest_vol']>0 else 0
    if not (blb >= 0.04 or jj_amount >= 15000000):
        return 0
    if jj_amount < 8000000:
        return 0

    score = 0
    # 高开+风险
    score += (pct**3)/10
    if pct > 6.5:
        score -= 5
    # 连板加分
    score += ind['zuo_lb']

    # 板形态
    bs = 0
    if not ind['yest_yz'] and ind['zuo_lb']>0:
        bs += 15
    if ind['yest_yz']:
        bs -= 10
    if 4 <= ind['yest_amp'] <= 13:
        bs += 8
    lb = ind['zuo_lb']
    bs += {2:3, 3:5, 4:6}.get(lb, 0)
    score += bs

    # 竞量昨比
    jzl = jj_vol / ind['yest_vol'] * 100 if ind['yest_vol']>0 else 0
    if jzl < 4: js = 0
    elif jzl < 5: js = 5
    elif jzl < 8: js = 10
    elif jzl < 12: js = 15
    elif jzl < 20: js = 18
    else: js = 10
    score += js

    # 金额评分
    if jj_amount < 20000000: ams = 3
    elif jj_amount < 30000000: ams = 5
    elif jj_amount < 50000000: ams = 8
    elif jj_amount < 80000000: ams = 11
    elif jj_amount < 120000000: ams = 13
    else: ams = 15
    score += ams

    # 昨日量能
    lns = -8 if ind['liangsuo'] else 8
    if ind['diliang']: lns -= 12
    if ind['juliang']: lns -= 10
    if ind['datie']: lns -= 15
    score += lns

    # 近期位置
    jq = ind['jq_zf']
    if jq < 10: jqs = 8
    elif jq < 15: jqs = 5
    elif jq < 20: jqs = 2
    elif jq < 25: jqs = 0
    else: jqs = -8
    score += jqs

    # 5日线偏离
    pl = (price - ind['ma5'])/ind['ma5']*100 if ind['ma5']>0 else 0
    if pl > 12: pls = 12
    elif pl > 8: pls = 6
    elif pl < 2: pls = -5
    else: pls = 0
    score += pls

    # 竞价流通强度
    lt_yz = row['流通市值'] * 10000
    lt_gu = lt_yz / price if price>0 else 0
    jj_qd = jj_vol * 100 / lt_gu * 100 if lt_gu>0 else 0
    if jj_qd < 0.85: ats = 0
    elif jj_qd < 1.5: ats = 5
    elif jj_qd < 3: ats = 10
    elif jj_qd < 5: ats = 15
    elif jj_qd < 8: ats = 18
    elif jj_qd < 12: ats = 15
    else: ats = 10
    score += ats

    # 量能异常扣分
    kf = 0
    if jj_vol>0 and (ind['yest_vol']*0.42 / jj_vol) > 30:
        kf -= 8
    if jzl > 25:
        kf -= 8
    score += kf

    # 市值评分
    lt_yi = row['流通市值'] / 10000
    if lt_yi < 100: szs = 8
    elif lt_yi < 200: szs = 6
    elif lt_yi < 400: szs = 4
    elif lt_yi < 600: szs = 2
    elif lt_yi < 1000: szs = 0
    else: szs = -8
    score += szs

    return round(score, 2)

def main():
    time.sleep(30)  # 延迟30秒，对应9:26:30
    today = datetime.now()
    if not is_trade_day(today):
        print("非交易日")
        return

    print("开始拉取行情...")
    df = get_stock_pool()
    if df.empty:
        send_feishu("【竞价选股】今日无符合初步条件标的")
        return

    s1, s2 = [], []
    for _, row in df.iterrows():
        ind = calc_indicators(row['代码'])
        if not ind:
            continue
        sc1 = score_s1(row, ind)
        sc2 = score_s2(row, ind)
        if sc1 > 0:
            s1.append({"名称":row['名称'], "代码":row['代码'], "价格":row['最新价'], "分":sc1})
        if sc2 > 0:
            s2.append({"名称":row['名称'], "代码":row['代码'], "价格":row['最新价'], "分":sc2})

    s1.sort(key=lambda x:x['分'], reverse=True)
    s2.sort(key=lambda x:x['分'], reverse=True)
    s1_top, s2_top = s1[:3], s2[:3]

    # 拼接消息
    msg = f"【竞价选股每日推送】\n日期：{today.strftime('%Y-%m-%d')}\n\n"
    msg += "===== 策略1：原始版轮回 前3名 =====\n"
    if not s1_top:
        msg += "今日无符合条件标的\n"
    else:
        for i,x in enumerate(s1_top,1):
            msg += f"{i}. {x['名称']}({x['代码']})  股价：{x['价格']}元  分值：{x['分']}\n"
    
    msg += "\n===== 策略2：轮回V2.4 前3名 =====\n"
    if not s2_top:
        msg += "今日无符合条件标的\n"
    else:
        for i,x in enumerate(s2_top,1):
            msg += f"{i}. {x['名称']}({x['代码']})  股价：{x['价格']}元  分值：{x['分']}\n"
    
    msg += "\n数据仅供研究参考，不构成投资建议"
    print(msg)
    send_feishu(msg)

if __name__ == "__main__":
    main()
