import plotly.graph_objects as go
from datetime import datetime, timedelta
import plotly.io as pio

def plot_ma_bands_web(closes, ma100s, threshold):
    closes = list(closes)
    ma100s = list(ma100s)

    now = datetime.now()
    times = [now - timedelta(minutes=len(closes)-i) for i in range(len(closes))]

    # None 값 방어 처리
    upper = [ma * (1 + threshold) if ma is not None else None for ma in ma100s]
    lower = [ma * (1 - threshold) if ma is not None else None for ma in ma100s]

    fig = go.Figure()

    fig.add_trace(go.Scatter(x=times, y=closes, mode="lines", name="Close"))
    fig.add_trace(go.Scatter(x=times, y=ma100s, mode="lines", name="MA100"))
    fig.add_trace(go.Scatter(x=times, y=upper, mode="lines", name="Upper", line=dict(dash="dot", color="green")))
    fig.add_trace(go.Scatter(x=times, y=lower, mode="lines", name="Lower", line=dict(dash="dot", color="red")))

    fig.update_layout(
        title="Close vs MA100 ± Threshold",
        xaxis_title="Time",
        yaxis_title="Price",
        hovermode="x unified"
    )

    # 브라우저로 바로 띄우기
    pio.renderers.default = "browser"
    fig.show()

# 사용 예시
plot_ma_bands_web(closes, ma100s, threshold=optimal)
