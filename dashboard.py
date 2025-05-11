import json
import streamlit as st
import pandas as pd
import plotly.express as px
from pathlib import Path

# 导入 codecarbon 输出的 emissions.csv
st.set_page_config(page_title="AI 编译组训练碳排放云表盘", layout="wide")
st.title("AI 训练任务碳排放可视化")

st.sidebar.header("路径设置")
log_dir = st.sidebar.text_input("emissions.csv 文件所在目录", value="carbon_logs")
log_file = Path(log_dir) / "emissions.csv"

if not log_file.exists():
    st.warning("未找到 emissions.csv 文件，请检查路径是否正确")
    st.stop()

# 读取数据
df = pd.read_csv(log_file)
df["timestamp"] = pd.to_datetime(df["timestamp"])
df = df.sort_values("timestamp").reset_index(drop=True)

# 选择某次实验进行分析
st.sidebar.header("实验选择")
experiment_options = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
selected_label = st.sidebar.selectbox("选择要分析的实验时间", experiment_options[::-1])  # 反转默认选择最近
selected_index = experiment_options.index(selected_label)
selected_row = df.iloc[selected_index]

# 可设置回顾历史实验数量
n_back = st.sidebar.slider("展示该实验之前的实验数量（含当前）", min_value=1, max_value=min(10, len(df)), value=3)
df_selected = df.iloc[max(0, selected_index - n_back + 1):selected_index + 1].copy()
df_selected["experiment"] = df_selected["timestamp"].dt.strftime("%m-%d %H:%M:%S")

# 当前实验配置摘要
st.subheader("📄 当前实验配置")
exp_info = {
    "运行时长 (s)": selected_row.get("duration", "N/A"),
    "硬件平台": selected_row.get("cpu_model", "N/A") + " + " + selected_row.get("gpu_model", "N/A"),
    "Python 版本": selected_row.get("python_version", "N/A"),
    "操作系统": selected_row.get("os", "N/A"),
    "电网区域": selected_row.get("grid_area", "N/A"),
    "碳排放因子": selected_row.get("grid_emission_factor", "N/A"),
    "操作模式": selected_row.get("grid_emission_factor_mode", "N/A"),
}
exp_col1, exp_col2 = st.columns(2)
with exp_col1:
    for k, v in list(exp_info.items())[:3]:
        st.markdown(f"**{k}**: {v}")
with exp_col2:
    for k, v in list(exp_info.items())[3:]:
        st.markdown(f"**{k}**: {v}")

# 统计信息（总能耗为所有记录的 energy_consumed 字段之和）
total_energy = df["energy_consumed"].sum()
total_emissions = df["emissions"].sum()

st.subheader("📊 总结")
col1, col2, col3 = st.columns(3)
col1.metric("总能耗 (kWh)", f"{total_energy:.3f}")
col2.metric("总碳排放 (kgCO₂eq)", f"{total_emissions:.3f}")
col3.metric("最后地区", selected_row.get("grid_area", "N/A"))

# 示意性换算
km = total_emissions * 1000 / 180  # 假设 180gCO2/km
st.info(f"总排放相当于开车 {km:.1f} 公里。")

# 可视化：动态选择实验的柱状图 + 饼图
st.subheader("🔍 指定实验的硬件碳排放分布")
bar_df = df_selected[["experiment", "cpu_energy", "gpu_energy", "ram_energy"]]
bar_df = bar_df.melt(id_vars="experiment", var_name="硬件组件", value_name="能耗 (kWh)")
fig_bar = px.bar(bar_df, x="experiment", y="能耗 (kWh)", color="硬件组件",
                 barmode="group", title="指定实验及其历史 CPU / GPU / RAM 能耗")

pie_parts = {
    "CPU": selected_row.get("cpu_energy", 0.0),
    "GPU": selected_row.get("gpu_energy", 0.0),
    "RAM": selected_row.get("ram_energy", 0.0),
}
fig_pie = px.pie(names=list(pie_parts.keys()), values=list(pie_parts.values()),
                 title="当前选中实验 CPU / GPU / RAM 能耗占比")

colb1, colb2 = st.columns(2)
colb1.plotly_chart(fig_bar, use_container_width=True)
colb2.plotly_chart(fig_pie, use_container_width=True)


# 碳排放时间走势（用户自定义查看数量，横轴使用序号避免时间间距问题）
st.subheader("📉 单次碳排放趋势（自定义次数，横轴为序号）")
n_curve = st.slider("显示最近 N 次实验的排放变化曲线", min_value=3, max_value=len(df), value=10)
df_curve = df.tail(n_curve).copy()
df_curve["实验序号"] = range(1, len(df_curve) + 1)
fig_time = px.line(df_curve, x="实验序号", y="emissions",
                   title="最近 N 次实验的碳排放变化 (kg)", markers=True)
st.plotly_chart(fig_time, use_container_width=True)

# 显示原始数据
toggle = st.checkbox("显示 emissions.csv 原始数据")
if toggle:
    st.dataframe(df)
    
# 🗺️ 地图高亮最近 N 次实验所在省份
st.subheader("🗺️ 最近实验涉及的运行地区（省份级）")

# 准备数据：统计最近 N 次实验的省份出现频次
province_counts = df_curve["region"].value_counts().reset_index()
province_counts.columns = ["省份英文名", "出现次数"]

# 中文映射：将英文拼写转为中文（可以视需要精细化）
province_en2zh_full = {
    "beijing": "北京市", "shanghai": "上海市", "guangdong": "广东省", "henan": "河南省",
    "shaanxi": "陕西省", "sichuan": "四川省", "liaoning": "辽宁省", "zhejiang": "浙江省",
    "jiangsu": "江苏省", "shandong": "山东省", "hebei": "河北省", "shanxi": "山西省",
    "anhui": "安徽省", "fujian": "福建省", "hubei": "湖北省", "hunan": "湖南省",
    "jiangxi": "江西省", "guangxi": "广西壮族自治区", "yunnan": "云南省", "guizhou": "贵州省",
    "hainan": "海南省", "tianjin": "天津市", "chongqing": "重庆市", "heilongjiang": "黑龙江省",
    "jilin": "吉林省", "gansu": "甘肃省", "qinghai": "青海省", "ningxia": "宁夏回族自治区",
    "xinjiang": "新疆维吾尔自治区", "neimenggu": "内蒙古自治区"
}

province_counts["省份"] = province_counts["省份英文名"].map(province_en2zh_full)

province_counts["是否高亮"] = 1  # 所有最近 N 次实验涉及的省份都高亮

# 全省份 DataFrame：用于显示未涉及省份的背景色
all_provinces = list(province_en2zh_full.values())
df_map = pd.DataFrame({"省份": all_provinces})
df_map["是否高亮"] = df_map["省份"].isin(province_counts["省份"]).astype(int)


# 加载本地 geojson（推荐使用阿里云的 100000_full.json）
with open("100000_full.json", "r", encoding="utf-8") as f:
    china_geo = json.load(f)

fig_map = px.choropleth(
    df_map,
    geojson=china_geo,
    featureidkey="properties.name",  # 非常关键，必须与你的 GeoJSON 字段匹配
    locations="省份",
    color="是否高亮",
    color_continuous_scale=[[0, "#dddddd"], [1, "#ff6347"]],
    range_color=(0, 1),
    title="最近 N 次实验涉及的省份（橙色高亮）"
)
fig_map.update_geos(fitbounds="locations", visible=False)
fig_map.update_layout(margin={"r":0,"t":40,"l":0,"b":0})
st.plotly_chart(fig_map, use_container_width=True)


st.caption("Copyright © 2025 Zhang Jiayuan | CodeCarbon Dashboard")
