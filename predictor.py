"""Bộ phân tích deterministic dùng chung cho bot. Không đảm bảo kết quả game may rủi."""
import re
from collections import Counter

class HashAnalyzer:
    @staticmethod
    def _bits(data):
        return [(b >> (7 - i)) & 1 for b in data for i in range(8)]

    @staticmethod
    def _entropy(values):
        if not values:
            return 0.0
        cnt = Counter(values)
        n = len(values)
        return -sum((v / n) * __import__('math').log2(v / n) for v in cnt.values())

    @staticmethod
    def _spectral_score(bits):
        # DFT thủ công, tránh thêm dependency và giữ tính tái lập.
        import math
        n = len(bits)
        if n < 16:
            return 0.0
        score = 0.0
        for k in range(1, min(n // 2, 32)):
            re_part = sum(bits[t] * math.cos(2 * math.pi * k * t / n) for t in range(n))
            im_part = sum(bits[t] * math.sin(2 * math.pi * k * t / n) for t in range(n))
            power = re_part * re_part + im_part * im_part
            score += power * (1 if k % 2 else -1)
        return score / (n * n)

    def _source_spectral_density(self, data):
        """Phần spectral density được trích từ md5bot.py, chỉ nhận bytes MD5."""
        if len(data) < 16:
            return 0.0, 0.0
        n = len(data)
        harmonics = []
        import math
        for k in range(n // 2):
            real = sum(data[t] * math.cos(2 * math.pi * k * t / n) for t in range(n))
            imag = sum(data[t] * math.sin(2 * math.pi * k * t / n) for t in range(n))
            harmonics.append(real * real + imag * imag)
        if not harmonics:
            return 0.0, 0.0
        total = sum(harmonics) + 1e-9
        odd_power = sum(harmonics[i] for i in range(1, len(harmonics), 2))
        even_power = sum(harmonics[i] for i in range(0, len(harmonics), 2))
        tai = xiu = 0.0
        spectral_bias = (odd_power - even_power) / total
        if spectral_bias > 0.15:
            tai += 16.0
        elif spectral_bias < -0.15:
            xiu += 16.0
        centroid = sum(i * power for i, power in enumerate(harmonics)) / total
        if centroid > len(harmonics) / 2:
            tai += 10.0
        else:
            xiu += 10.0
        return tai, xiu

    def _source_cellular_rule30(self, data):
        """Rule 30 trong md5bot.py, giữ độc lập với các phần kết nối bên ngoài."""
        if len(data) < 16:
            return 0.0, 0.0
        bits = self._bits(data)
        state = list(bits)
        density_history = []
        for _ in range(8):
            state = [state[(i - 1) % len(state)] ^ (state[i] | state[(i + 1) % len(state)]) for i in range(len(state))]
            density_history.append(sum(state) / len(state))
        tai = xiu = 0.0
        avg_density = sum(density_history) / len(density_history)
        if avg_density > 0.52:
            tai += 18.0
        elif avg_density < 0.48:
            xiu += 18.0
        if density_history[-1] > density_history[0]:
            tai += 8.0
        else:
            xiu += 8.0
        return tai, xiu

    def _source_ultimate_md5_core(self, data):
        """Lõi ultimate_md5_core_v4 đã tách riêng khỏi các tính năng VIP khác."""
        if len(data) < 16:
            return 0.0, 0.0, []
        import math
        n = len(data)
        tai = xiu = 0.0
        details = []
        nibbles = [(b >> 4) & 0xF for b in data] + [b & 0xF for b in data]
        high_nib = sum(1 for nib in nibbles if nib >= 8)
        low_nib = len(nibbles) - high_nib
        if high_nib > low_nib * 1.15:
            tai += 15.0; details.append('v4-high-nibble→Tài')
        elif low_nib > high_nib * 1.15:
            xiu += 15.0; details.append('v4-low-nibble→Xỉu')
        even_nib = sum(1 for nib in nibbles if nib % 2 == 0)
        odd_nib = len(nibbles) - even_nib
        if even_nib > odd_nib * 1.2:
            xiu += 8.0
        elif odd_nib > even_nib * 1.2:
            tai += 8.0

        byte_counts = Counter(data)
        entropy = 0.0
        for count in byte_counts.values():
            p = count / n
            if p > 0:
                entropy -= p * math.log2(p)
        max_entropy = math.log2(min(256, n))
        entropy_ratio = entropy / max_entropy if max_entropy else 0.0
        if entropy_ratio > 0.96:
            if data[-1] >= 128: tai += 18.0
            else: xiu += 18.0
        elif entropy_ratio < 0.85:
            if sum(data) / n > 128: xiu += 18.0
            else: tai += 18.0

        bits = self._bits(data)
        ones = sum(bits); zeros = len(bits) - ones
        if ones > zeros + 10: xiu += 12.0
        elif zeros > ones + 10: tai += 12.0
        runs = []; run = 1
        for i in range(1, len(bits)):
            if bits[i] == bits[i - 1]: run += 1
            else: runs.append(run); run = 1
        runs.append(run)
        avg_run = sum(runs) / len(runs)
        if avg_run > 2.5: tai += 10.0
        elif avg_run < 1.8: xiu += 10.0

        transitions = Counter()
        for i in range(len(nibbles) - 2):
            pair = (nibbles[i] >= 8, nibbles[i + 1] >= 8)
            transitions[(pair, nibbles[i + 2] >= 8)] += 1
        last_pair = (nibbles[-2] >= 8, nibbles[-1] >= 8)
        if transitions[(last_pair, True)] > transitions[(last_pair, False)]:
            tai += 20.0
        elif transitions[(last_pair, False)] > transitions[(last_pair, True)]:
            xiu += 20.0

        spectral_tai, spectral_xiu = self._source_spectral_density(data)
        cellular_tai, cellular_xiu = self._source_cellular_rule30(data)
        tai += spectral_tai + cellular_tai
        xiu += spectral_xiu + cellular_xiu
        if tai > xiu + 5:
            details.append('v4-core→Tài')
        elif xiu > tai + 5:
            details.append('v4-core→Xỉu')
        return tai, xiu, details

    def analyze(self, value):
        raw = re.sub(r"\s+", "", value or "").lower()
        if not re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", raw):
            return {"ok": False, "error": "Mã phải là MD5 32 ký tự hoặc SHA-256 64 ký tự hệ hex."}
        data = bytes.fromhex(raw)
        bits = self._bits(data)
        score_tai = 50.0
        score_xiu = 50.0
        details = []

        # Bổ sung lõi dự đoán v4 từ md5bot.py; không thay đổi các luồng
        # key, nạp tiền, tài khoản hoặc quản trị của bot hiện tại.
        v4_tai, v4_xiu, v4_details = self._source_ultimate_md5_core(data)
        score_tai += v4_tai
        score_xiu += v4_xiu
        details.extend(v4_details)

        # 1. Nibble/high-low score, lấy trực tiếp tinh thần ultimate_md5_core_v4.
        nibbles = [(b >> 4) & 15 for b in data] + [b & 15 for b in data]
        high = sum(1 for n in nibbles if n >= 8)
        low = len(nibbles) - high
        if high > low * 1.15:
            score_tai += 8; details.append("high-nibble→Tài")
        elif low > high * 1.15:
            score_xiu += 8; details.append("low-nibble→Xỉu")

        odd = sum(n % 2 for n in nibbles)
        even = len(nibbles) - odd
        if odd > even * 1.20:
            score_tai += 5
        elif even > odd * 1.20:
            score_xiu += 5

        # 2. Shannon entropy và phân bố byte.
        ent = self._entropy(data)
        ratio = ent / 8.0
        if ratio > 0.90:
            (score_tai if data[-1] >= 128 else score_xiu)
            if data[-1] >= 128: score_tai += 6
            else: score_xiu += 6
            details.append("entropy-cao")
        elif ratio < 0.70:
            if sum(data) / len(data) >= 128: score_xiu += 6
            else: score_tai += 6
            details.append("entropy-thap")

        # 3. Bit ratio và độ dài run.
        ones = sum(bits); zeros = len(bits) - ones
        if zeros > ones + 10: score_tai += 6
        elif ones > zeros + 10: score_xiu += 6
        runs = []
        run = 1
        for i in range(1, len(bits)):
            if bits[i] == bits[i - 1]: run += 1
            else: runs.append(run); run = 1
        runs.append(run)
        avg_run = sum(runs) / max(1, len(runs))
        if avg_run > 2.5: score_tai += 5
        elif avg_run < 1.8: score_xiu += 5

        # 4. Markov 2-step trên chuỗi nibble cao/thấp trong chính hash.
        trans = Counter()
        highbits = [int(n >= 8) for n in nibbles]
        for i in range(len(highbits) - 2):
            trans[(highbits[i], highbits[i + 1], highbits[i + 2])] += 1
        last = tuple(highbits[-2:])
        t = trans[(last[0], last[1], 1)]
        x = trans[(last[0], last[1], 0)]
        if t > x: score_tai += 7
        elif x > t: score_xiu += 7

        # 5. Fourier-like deterministic spectral score.
        spectral = self._spectral_score(bits)
        if spectral > 0.002: score_tai += 5; details.append("spectral→Tài")
        elif spectral < -0.002: score_xiu += 5; details.append("spectral→Xỉu")

        # 6. Cellular automaton Rule 30, cùng ý tưởng trong file mẫu.
        state = bits[:]
        for _ in range(8):
            state = [state[(i - 1) % len(state)] ^ (state[i] | state[(i + 1) % len(state)]) for i in range(len(state))]
        density = sum(state) / len(state)
        if density > 0.52: score_tai += 5
        elif density < 0.48: score_xiu += 5

        total = score_tai + score_xiu
        tai = round(score_tai / total * 100, 1)
        xiu = round(score_xiu / total * 100, 1)
        result = "Tài" if tai >= xiu else "Xỉu"
        confidence = round(max(tai, xiu), 1)
        return {"ok": True, "hash": raw, "result": result, "tai": tai, "xiu": xiu,
                "confidence": confidence, "detail": ", ".join(details) or "tổng hợp deterministic"}


def predict_sessions(sessions):
    """Deep Brain ensemble đối xứng cho chuỗi phiên mới -> cũ.
    Tái hiện các lớp chính của engine.php: runs/survival, khuôn cầu,
    Markov nhiều bậc, EWMA, cân bằng, k-NN, logistic online, mirror vote
    và streak governor. Chỉ là tham khảo, không bảo đảm game ngẫu nhiên.
    """
    rows=[x for x in sessions if isinstance(x,dict) and x.get('res') in ('T','X')]
    if len(rows)<4: return {'ok':False,'error':'Cần tối thiểu 4 phiên hợp lệ'}
    seq=[x['res'] for x in rows[:160]]; tot=[int(x.get('total') or 0) for x in rows[:160]]
    flip=lambda x:'X' if x=='T' else 'T'
    def runs(a):
        out=[]
        for v in a:
            if out and out[-1][0]==v: out[-1]=(v,out[-1][1]+1)
            else: out.append((v,1))
        return out
    def collect(a,t):
        n=len(a); cur=a[0]; anti=flip(cur); rr=runs(a); streak=rr[0][1]; votes=[]
        def add(name,p,w):
            if p in ('T','X') and w>.02: votes.append((name,p,float(w)))
        # survival + streak governor evidence
        lens=[z[1] for z in rr[1:]]; tries=sum(x>=streak for x in lens); stop=sum(x==streak for x in lens)
        if tries>=3:
            surv=(tries-stop+.5)/(tries+1.0); add('survival',cur if surv>=.55 else anti,.9+abs(surv-.5)*2.6)
        if streak>=4:
            prior=max(.18,.5-(streak-3)*.065); pcont=(sum(x>streak for x in lens)+2*prior)/(tries+2) if tries else prior
            add('streak-governor',cur if pcont>=.46 else anti,1.55+abs(pcont-.5)*1.8)
        # exact run shape library
        shapes={(1,1):1.5,(2,2):1.45,(3,3):1.6,(4,4):1.5,(1,2):1.25,(2,1):1.25,(1,3):1.35,(3,1):1.35,(2,1,2):1.7,(3,1,3):1.75,(1,2,3):1.4,(3,2,1):1.4}
        sig=tuple(z[1] for z in rr)
        for pat,w in shapes.items():
            L=len(pat)
            if len(sig)>=L+2 and tuple(sig[1:1+L])==pat:
                add('shape-'+'-'.join(map(str,pat)),cur if streak<pat[0] else anti,w)
        # Markov 1..6
        for order,base in ((1,1.15),(2,1.35),(3,1.25),(4,1.15),(5,1.05),(6,.95)):
            if n<=order+5: continue
            key=tuple(a[:order]); c={'T':0,'X':0}
            for i in range(n-order):
                if tuple(a[i+1:i+1+order])==key: c[a[i]]+=1
            z=c['T']+c['X']
            if z>=3 and c['T']!=c['X']:
                p=(max(c.values())+.5)/(z+1); add('markov-'+str(order),'T' if c['T']>c['X'] else 'X',base*(p-.5)*3*min(1,z/8))
        # recency + multi-window frequency
        for size,w in ((6,1.15),(12,1.35),(24,1.15),(48,.8)):
            c=Counter(a[:min(size,n)])
            if c['T']!=c['X']: add('freq-'+str(size),'T' if c['T']>c['X'] else 'X',w*(1+abs(c['T']-c['X'])/max(1,min(size,n))))
        ew=0; den=0
        for i,v in enumerate(a[:24]):
            k=__import__('math').exp(-i/6); ew+=(1 if v=='T' else -1)*k; den+=k
        m=ew/den if den else 0
        if abs(m)>.2: add('EWMA', 'T' if m>0 else 'X',.65+min(.8,abs(m)*1.3))
        if abs(m)>.68: add('mean-reversion','X' if m>0 else 'T',.6+min(.65,(abs(m)-.68)*2))
        # switch regime / alternation
        if n>=8:
            sw=sum(a[i]!=a[i+1] for i in range(min(20,n-1)))/max(1,min(20,n-1))
            if sw>.66: add('switch-regime',anti,1.2)
            elif sw<.34: add('trend-regime',cur,1.2)
        # total score around 10.5
        vals=[x for x in t[:16] if x>0]
        if len(vals)>=6:
            bias=sum((v-10.5)*__import__('math').exp(-i/5) for i,v in enumerate(vals))/sum(__import__('math').exp(-i/5) for i in range(len(vals)))
            if abs(bias)>.4: add('score-center','T' if bias>0 else 'X',.6+min(.7,abs(bias)/3))
            if abs(bias)>2.5: add('score-reversion','X' if bias>0 else 'T',.55)
        # kNN current pattern
        if n>=24:
            L=8; cur8=a[:L]; near=[]
            for i in range(1,n-L):
                d=sum(cur8[k]!=a[i+k] for k in range(L)); near.append((d,a[i-1]))
            for d,v in sorted(near)[:9]: add('knn-8',v,1/(1+d*d)*.9)
        return votes
    A=collect(seq,tot); B=collect([flip(v) for v in seq],[21-v if v else 0 for v in tot])
    scores={'T':0.0,'X':0.0}; modelbag={}
    for name,p,w in A+B:
        pp=flip(p) if (name in {x[0] for x in B} and False) else p
        # B was calculated on mirrored labels, therefore flip its vote back by position.
    for name,p,w in A: scores[p]+=w; modelbag.setdefault(name,{'T':0,'X':0}); modelbag[name][p]+=w
    for name,p,w in B: p=flip(p); scores[p]+=w; modelbag.setdefault(name,{'T':0,'X':0}); modelbag[name][p]+=w
    pick='T' if scores['T']>=scores['X'] else 'X'; total_score=sum(scores.values()); edge=abs(scores['T']-scores['X'])/max(total_score,1)
    allvotes=A+[(n,flip(p),w) for n,p,w in B]; agree=sum(w for _,p,w in allvotes if p==pick); ratio=agree/max(sum(w for _,_,w in allvotes),1)
    rr=runs(seq); streak=rr[0][1]; pcont=.5
    if streak>=4:
        lens=[z[1] for z in rr[1:]]; tries=sum(x>=streak for x in lens); go=sum(x>streak for x in lens); prior=max(.18,.5-(streak-3)*.065); pcont=(go+2*prior)/(tries+2) if tries else prior
        if pick==seq[0] and pcont<.46: pick=flip(pick); edge=max(edge,(.5-pcont)*2.4)
        elif pick==seq[0]: edge*=.5+pcont
    conf=int(round(max(52,min(92,50+edge*46+max(0,ratio-.5)*18))))
    shape='-'.join(str(x[1]) for x in rr[:5]); top=sorted(modelbag.items(),key=lambda kv:max(kv[1].values()),reverse=True)[:5]
    reason='DEEP BRAIN v26 Python · đối xứng mirror · '+str(len(allvotes))+' tín hiệu · đồng thuận '+str(round(ratio*100))+'% · cầu '+shape
    if streak>=4: reason+=f' · streak governor {round(pcont*100)}% đi tiếp'
    return {'ok':True,'sid':rows[0].get('sid',0),'next':rows[0].get('sid',0)+1 if rows[0].get('sid',0) else 0,'pick':pick,'label':'Tài' if pick=='T' else 'Xỉu','conf':conf,'last':rows[0].get('res'),'total':rows[0].get('total',0),'history':seq[:30],'reason':reason}


class SuperPredictor:
    """Facade thống nhất cho hash và chuỗi phiên."""
    def __init__(self): self.hash=HashAnalyzer()
    def analyze_hash(self, value): return self.hash.analyze(value)
    def analyze_sessions(self, sessions): return predict_sessions(sessions)
