import streamlit as st
import pandas as pd
import json
from pathlib import Path
import mysql.connector

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

SCHEMA_FILE = Path(__file__).parent / "schema.json"
AGG_OPTIONS = ["(無)", "COUNT", "COUNT DISTINCT", "SUM", "AVG", "MAX", "MIN"]
OPERATORS   = ["=", "!=", ">", "<", ">=", "<=", "LIKE", "IN", "BETWEEN", "IS NULL", "IS NOT NULL"]
JOIN_TYPES  = ["LEFT JOIN", "INNER JOIN", "RIGHT JOIN"]

MYSQL_CONFIG = {
    "host": "sunggang-instance-1.cxt9jose8yw6.ap-northeast-1.rds.amazonaws.com",
    "port": 3306,
    "database": "TableauDashboard",
    "user": "for_tableau",
    "charset": "utf8mb4",
}


def execute_query(sql: str) -> pd.DataFrame:
    sql_upper = sql.strip().upper()
    if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
        raise ValueError("安全限制：只允許執行 SELECT 查詢。")
    conn = mysql.connector.connect(
        **MYSQL_CONFIG,
        password=st.secrets["MYSQL_PASSWORD"],
    )
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql)
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def show_query_results(df: pd.DataFrame, key_prefix: str):
    if df.empty:
        st.warning("查無符合條件的資料。")
        return
    if df.shape == (1, 1):
        val = df.iloc[0, 0]
        st.metric(label=df.columns[0], value=f"{val:,}" if isinstance(val, (int, float)) else val)
    else:
        st.markdown(f"**查詢結果（共 {len(df):,} 筆）**")
        st.dataframe(df, use_container_width=True)
    csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    st.download_button(
        label="📥 下載 CSV",
        data=csv_bytes,
        file_name="query_result.csv",
        mime="text/csv",
        key=f"{key_prefix}_csv_dl",
    )

# ──────────────────────────────────────────────────────────────────────────────
# Page setup
# ──────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="SQL 翻譯機", page_icon="🔡", layout="wide")
st.title("🔡 SQL 翻譯機")
st.caption("上傳 CSV 定義資料表結構，透過選單操作自動產生 MySQL / Tableau SQL。")

# ──────────────────────────────────────────────────────────────────────────────
# Schema helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_schema() -> dict:
    if "schema" not in st.session_state:
        if SCHEMA_FILE.exists():
            st.session_state.schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
        else:
            st.session_state.schema = {}
    return st.session_state.schema


def persist_schema():
    SCHEMA_FILE.write_text(
        json.dumps(st.session_state.schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ──────────────────────────────────────────────────────────────────────────────
# SQL generation
# ──────────────────────────────────────────────────────────────────────────────

def _col_expr(col: str, agg: str, tbl_alias: str = None, out_alias: str = None) -> str:
    ref = f"`{tbl_alias}`.`{col}`" if tbl_alias else f"`{col}`"
    if agg == "COUNT DISTINCT":
        expr = f"COUNT(DISTINCT {ref})"
    elif agg and agg != "(無)":
        expr = f"{agg}({ref})"
    else:
        expr = ref
    if out_alias:
        expr += f" AS `{out_alias}`"
    return expr


def _where_clause(conds: list) -> str:
    if not conds:
        return ""
    lines = []
    for i, c in enumerate(conds):
        tbl = c.get("tbl")
        ref = f"`{tbl}`.`{c['col']}`" if tbl else f"`{c['col']}`"
        op, val = c["op"], c.get("val", "")

        if op in ("IS NULL", "IS NOT NULL"):
            expr = f"{ref} {op}"
        elif op == "IN":
            items = ", ".join(f"'{v.strip()}'" for v in val.split(",") if v.strip())
            expr = f"{ref} IN ({items})"
        elif op == "BETWEEN":
            v = [(x.strip() or "0") for x in (val + ",").split(",")]
            expr = f"{ref} BETWEEN '{v[0]}' AND '{v[1]}'"
        elif op == "LIKE":
            expr = f"{ref} LIKE '{val}'"
        else:
            is_num = val.lstrip("-").replace(".", "", 1).isdigit() if val else False
            expr = f"{ref} {op} {val}" if is_num else f"{ref} {op} '{val}'"

        prefix = "" if i == 0 else (c.get("connector", "AND") + " ")
        lines.append(f"  {prefix}{expr}")

    return "WHERE\n" + "\n".join(lines)


def gen_single(table, col_cfgs, conds, group_by, order_by, limit) -> str:
    if col_cfgs:
        selects = ",\n".join(
            "  " + _col_expr(c["col"], c.get("agg", "(無)"), out_alias=c.get("alias") or None)
            for c in col_cfgs
        )
        select = f"SELECT\n{selects}"
    else:
        select = "SELECT *"

    parts = [select, f"FROM `{table}`"]

    w = _where_clause(conds)
    if w:
        parts.append(w)

    if group_by:
        parts.append("GROUP BY\n" + ",\n".join(f"  `{c}`" for c in group_by))

    if order_by:
        parts.append("ORDER BY\n" + ",\n".join(f"  `{o['col']}` {o['dir']}" for o in order_by))

    if limit:
        parts.append(f"LIMIT {limit}")

    return "\n".join(parts)


def gen_join(tables, joins, col_cfgs, conds, group_by, order_by, limit) -> str:
    if col_cfgs:
        selects = ",\n".join(
            "  " + _col_expr(c["col"], c.get("agg", "(無)"), tbl_alias=c.get("tbl"), out_alias=c.get("alias") or None)
            for c in col_cfgs
        )
        select = f"SELECT\n{selects}"
    else:
        select = "SELECT *"

    main = tables[0]
    main_alias = main.get("alias") or main["name"]
    use_alias = main_alias != main["name"]
    from_ = f"FROM `{main['name']}`" + (f" AS `{main_alias}`" if use_alias else "")

    parts = [select, from_]

    for j in joins:
        right_name = next(
            (t["name"] for t in tables if (t.get("alias") or t["name"]) == j["right_alias"]),
            j["right_alias"],
        )
        parts.append(
            f"{j['type']} `{right_name}` AS `{j['right_alias']}`\n"
            f"  ON `{j['left_alias']}`.`{j['left_key']}` = `{j['right_alias']}`.`{j['right_key']}`"
        )

    w = _where_clause(conds)
    if w:
        parts.append(w)

    if group_by:
        gb_lines = []
        for g in group_by:
            tbl = g.get("tbl")
            gb_lines.append(f"  `{tbl}`.`{g['col']}`" if tbl else f"  `{g['col']}`")
        parts.append("GROUP BY\n" + ",\n".join(gb_lines))

    if order_by:
        ob_lines = []
        for o in order_by:
            col = o["col"]
            if "." in col:
                t, c = col.split(".", 1)
                ob_lines.append(f"  `{t}`.`{c}` {o['dir']}")
            else:
                ob_lines.append(f"  `{col}` {o['dir']}")
        parts.append("ORDER BY\n" + ",\n".join(ob_lines))

    if limit:
        parts.append(f"LIMIT {limit}")

    return "\n".join(parts)


def wrap_tableau(sql: str) -> str:
    return f"(\n{sql}\n) AS custom_sql"

# ──────────────────────────────────────────────────────────────────────────────
# Reusable UI components
# ──────────────────────────────────────────────────────────────────────────────

def condition_builder(prefix: str, col_options: list) -> list:
    key = f"{prefix}_conds"
    if key not in st.session_state:
        st.session_state[key] = []
    conds = st.session_state[key]

    if st.button("＋ 新增篩選條件", key=f"{prefix}_add_cond"):
        default = col_options[0] if col_options else ""
        entry = {"full": default, "op": "=", "val": "", "connector": "AND"}
        if "." in default:
            entry["tbl"], entry["col"] = default.split(".", 1)
        else:
            entry["tbl"], entry["col"] = None, default
        conds.append(entry)

    to_del = []
    for i, c in enumerate(conds):
        st.markdown(f"<small>條件 {i + 1}</small>", unsafe_allow_html=True)

        if i > 0:
            row = st.columns([1, 2, 1, 2, 1])
            with row[0]:
                c["connector"] = st.selectbox(
                    "連接", ["AND", "OR"], key=f"{prefix}_conn_{i}", label_visibility="collapsed"
                )
            offset = 1
        else:
            row = st.columns([2, 1, 2, 1])
            offset = 0

        with row[offset]:
            idx = col_options.index(c.get("full", "")) if c.get("full", "") in col_options else 0
            full = st.selectbox("欄位", col_options, index=idx, key=f"{prefix}_col_{i}", label_visibility="collapsed")
            c["full"] = full
            if "." in full:
                c["tbl"], c["col"] = full.split(".", 1)
            else:
                c["tbl"], c["col"] = None, full

        with row[offset + 1]:
            c["op"] = st.selectbox("運算子", OPERATORS, key=f"{prefix}_op_{i}", label_visibility="collapsed")

        if c["op"] not in ("IS NULL", "IS NOT NULL"):
            with row[offset + 2]:
                hint = "用逗號分隔多個值" if c["op"] in ("IN", "BETWEEN") else ""
                c["val"] = st.text_input(
                    "值", value=c.get("val", ""), placeholder=hint,
                    key=f"{prefix}_val_{i}", label_visibility="collapsed",
                )

        with row[-1]:
            if st.button("✕", key=f"{prefix}_del_{i}"):
                to_del.append(i)

    for i in reversed(to_del):
        conds.pop(i)

    st.session_state[key] = conds
    return conds


def orderby_builder(prefix: str, col_options: list) -> list:
    key = f"{prefix}_orderby"
    if key not in st.session_state:
        st.session_state[key] = []
    orders = st.session_state[key]

    if st.button("＋ 新增排序欄位", key=f"{prefix}_add_ob"):
        orders.append({"col": col_options[0] if col_options else "", "dir": "ASC"})

    to_del = []
    for i, o in enumerate(orders):
        c1, c2, c3 = st.columns([3, 2, 1])
        with c1:
            idx = col_options.index(o["col"]) if o["col"] in col_options else 0
            o["col"] = st.selectbox("欄位", col_options, index=idx, key=f"{prefix}_obcol_{i}", label_visibility="collapsed")
        with c2:
            o["dir"] = st.selectbox("方向", ["ASC", "DESC"], key=f"{prefix}_obdir_{i}", label_visibility="collapsed")
        with c3:
            if st.button("✕", key=f"{prefix}_obdel_{i}"):
                to_del.append(i)

    for i in reversed(to_del):
        orders.pop(i)

    st.session_state[key] = orders
    return orders

# ──────────────────────────────────────────────────────────────────────────────
# Sidebar — schema management
# ──────────────────────────────────────────────────────────────────────────────

schema = load_schema()

with st.sidebar:
    st.header("📂 資料表管理")

    files = st.file_uploader(
        "上傳 CSV（檔名即 Table 名稱）",
        type="csv",
        accept_multiple_files=True,
        help="僅讀取欄位標題，不會上傳實際資料內容",
    )
    if files:
        for f in files:
            tbl_name = Path(f.name).stem
            df = pd.read_csv(f, nrows=0)
            schema[tbl_name] = list(df.columns)
        persist_schema()
        st.success(f"✅ 已載入 {len(files)} 個資料表")
        st.rerun()

    if schema:
        st.divider()
        st.subheader("已載入的資料表")
        for tbl, cols in list(schema.items()):
            with st.expander(f"📋 {tbl}（{len(cols)} 欄）"):
                for c in cols:
                    st.text(f"  • {c}")
                if st.button("🗑️ 刪除此資料表", key=f"del_{tbl}"):
                    del schema[tbl]
                    persist_schema()
                    st.rerun()
    else:
        st.info("尚未載入任何資料表\n請上傳 CSV 開始使用")

# ──────────────────────────────────────────────────────────────────────────────
# Guard
# ──────────────────────────────────────────────────────────────────────────────

if not schema:
    st.warning("⚠️ 請先在左側上傳 CSV 檔案來定義資料表結構。")
    st.stop()

table_names = list(schema.keys())

# ──────────────────────────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4 = st.tabs(["📋 單表查詢", "🔗 多表 JOIN", "➕ UNION 合併", "🤖 查詢 Agent"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Single table
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    table = st.selectbox("資料表", table_names, key="s_table")
    cols_avail = schema[table]

    # Columns + aggregation
    st.markdown("##### 欄位與聚合方式")
    sel_cols = st.multiselect("選擇欄位（不選則代表 SELECT *）", cols_avail, key="s_sel_cols")

    col_cfgs = []
    if sel_cols:
        hdr = st.columns([2, 2, 2])
        hdr[0].caption("欄位名稱")
        hdr[1].caption("聚合方式")
        hdr[2].caption("輸出別名（選填）")
        for col in sel_cols:
            r = st.columns([2, 2, 2])
            r[0].markdown(f"**{col}**")
            with r[1]:
                agg = st.selectbox("聚合", AGG_OPTIONS, key=f"s_agg_{col}", label_visibility="collapsed")
            with r[2]:
                alias = st.text_input("別名", key=f"s_alias_{col}", placeholder="選填", label_visibility="collapsed")
            col_cfgs.append({"col": col, "agg": agg, "alias": alias or None})

    # WHERE
    st.markdown("---\n##### 篩選條件（WHERE）")
    conds = condition_builder("s", cols_avail)

    # GROUP BY
    st.markdown("---\n##### 群組彙總（GROUP BY）")
    group_by = st.multiselect("選擇 GROUP BY 欄位", cols_avail, key="s_groupby")

    # ORDER BY
    st.markdown("---\n##### 排序（ORDER BY）")
    order_by = orderby_builder("s", cols_avail)

    # LIMIT
    st.markdown("---\n##### 筆數限制（LIMIT）")
    limit_val = st.number_input("LIMIT（0 = 不限制）", min_value=0, value=0, step=100, key="s_limit")

    # Output
    st.divider()
    fmt = st.radio("輸出格式", ["MySQL 標準語法", "Tableau 自訂 SQL"], horizontal=True, key="s_fmt")

    if st.button("🔨 產生 SQL", type="primary", key="s_gen"):
        sql = gen_single(table, col_cfgs, conds, group_by, order_by, limit_val or None)
        if fmt == "Tableau 自訂 SQL":
            sql = wrap_tableau(sql)
        st.session_state.s_sql = sql

    if "s_sql" in st.session_state:
        st.markdown("##### ✅ 產生的 SQL（右上角可複製）")
        st.code(st.session_state.s_sql, language="sql")
        if st.button("▶ 執行查詢", key="s_exec"):
            with st.spinner("查詢中..."):
                try:
                    st.session_state.s_df = execute_query(st.session_state.s_sql)
                except Exception as e:
                    st.error(f"查詢失敗：{e}")
        if "s_df" in st.session_state:
            show_query_results(st.session_state.s_df, "s")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — JOIN
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    if "j_tables" not in st.session_state:
        st.session_state.j_tables = [{"name": table_names[0], "alias": table_names[0]}]
    if "j_joins" not in st.session_state:
        st.session_state.j_joins = []

    j_tables = st.session_state.j_tables
    j_joins  = st.session_state.j_joins

    # Main table
    st.markdown("##### 主表設定")
    c1, c2 = st.columns(2)
    with c1:
        j_tables[0]["name"] = st.selectbox(
            "主表", table_names,
            index=table_names.index(j_tables[0]["name"]) if j_tables[0]["name"] in table_names else 0,
            key="j_main_tbl",
        )
    with c2:
        entered_alias = st.text_input("別名（AS）", value=j_tables[0].get("alias", j_tables[0]["name"]), key="j_main_alias")
        j_tables[0]["alias"] = entered_alias or j_tables[0]["name"]

    # Add JOIN button
    if st.button("＋ 新增 JOIN 表", key="j_add_tbl"):
        idx = len(j_tables)
        new_tbl = table_names[0]
        new_al  = f"t{idx}"
        j_tables.append({"name": new_tbl, "alias": new_al})
        prev     = j_tables[idx - 1]
        lft_cols = schema.get(prev["name"], [])
        rgt_cols = schema.get(new_tbl, [])
        j_joins.append({
            "type":        "LEFT JOIN",
            "left_alias":  prev.get("alias", prev["name"]),
            "left_key":    lft_cols[0] if lft_cols else "",
            "right_alias": new_al,
            "right_key":   rgt_cols[0] if rgt_cols else "",
        })

    # Each JOIN table
    to_del_jt = []
    for i in range(1, len(j_tables)):
        t = j_tables[i]
        j = j_joins[i - 1]

        st.markdown(f"---\n##### JOIN 表 {i}")
        c1, c2, c3 = st.columns(3)
        with c1:
            t["name"] = st.selectbox(
                "資料表", table_names,
                index=table_names.index(t["name"]) if t["name"] in table_names else 0,
                key=f"j_tbl_{i}",
            )
        with c2:
            entered = st.text_input("別名（AS）", value=t.get("alias", f"t{i}"), key=f"j_alias_{i}")
            t["alias"] = entered or f"t{i}"
        with c3:
            j["type"] = st.selectbox("JOIN 類型", JOIN_TYPES, key=f"j_type_{i}")

        # Always sync aliases from current table state
        j["left_alias"]  = j_tables[i - 1].get("alias", j_tables[i - 1]["name"])
        j["right_alias"] = t["alias"]

        left_cols  = schema.get(j_tables[i - 1]["name"], [])
        right_cols = schema.get(t["name"], [])

        st.markdown("**關聯鍵設定（ON）**")
        k1, k2, k3 = st.columns([5, 1, 5])
        with k1:
            lk_idx = left_cols.index(j["left_key"]) if j.get("left_key") in left_cols else 0
            j["left_key"] = st.selectbox(
                f"{j['left_alias']} 的關聯欄位", left_cols, index=lk_idx, key=f"j_lk_{i}"
            )
        with k2:
            st.markdown("<br><center style='font-size:1.2rem'>=</center>", unsafe_allow_html=True)
        with k3:
            rk_idx = right_cols.index(j["right_key"]) if j.get("right_key") in right_cols else 0
            j["right_key"] = st.selectbox(
                f"{j['right_alias']} 的關聯欄位", right_cols, index=rk_idx, key=f"j_rk_{i}"
            )

        if st.button(f"移除 JOIN 表 {i}", key=f"j_del_{i}"):
            to_del_jt.append(i)

    for i in reversed(to_del_jt):
        j_tables.pop(i)
        if i - 1 < len(j_joins):
            j_joins.pop(i - 1)

    # All available columns across joined tables
    all_j_cols = []
    for t in j_tables:
        al = t.get("alias") or t["name"]
        for c in schema.get(t["name"], []):
            all_j_cols.append(f"{al}.{c}")

    # Column selection
    st.markdown("---\n##### 選擇輸出欄位")
    sel_j_cols = st.multiselect("選擇欄位（格式：表別名.欄位）", all_j_cols, key="j_sel_cols")

    j_col_cfgs = []
    if sel_j_cols:
        hdr = st.columns([1, 1, 2, 2])
        hdr[0].caption("表別名")
        hdr[1].caption("欄位名稱")
        hdr[2].caption("聚合方式")
        hdr[3].caption("輸出別名（選填）")
        for fc in sel_j_cols:
            tbl_al, col = fc.split(".", 1)
            r = st.columns([1, 1, 2, 2])
            r[0].markdown(f"**{tbl_al}**")
            r[1].text(col)
            with r[2]:
                agg = st.selectbox("聚合", AGG_OPTIONS, key=f"j_agg_{fc}", label_visibility="collapsed")
            with r[3]:
                alias = st.text_input("別名", key=f"j_cal_{fc}", placeholder="選填", label_visibility="collapsed")
            j_col_cfgs.append({"tbl": tbl_al, "col": col, "agg": agg, "alias": alias or None})

    # WHERE
    st.markdown("---\n##### 篩選條件（WHERE）")
    j_conds = condition_builder("j", all_j_cols)

    # GROUP BY
    st.markdown("---\n##### 群組彙總（GROUP BY）")
    j_groupby_raw = st.multiselect("選擇 GROUP BY 欄位", all_j_cols, key="j_groupby")
    j_group_by = []
    for fg in j_groupby_raw:
        if "." in fg:
            tbl_al, col = fg.split(".", 1)
            j_group_by.append({"tbl": tbl_al, "col": col})
        else:
            j_group_by.append({"tbl": None, "col": fg})

    # ORDER BY
    st.markdown("---\n##### 排序（ORDER BY）")
    j_order_by = orderby_builder("j", all_j_cols)

    # LIMIT
    st.markdown("---\n##### 筆數限制（LIMIT）")
    j_limit = st.number_input("LIMIT（0 = 不限制）", min_value=0, value=0, step=100, key="j_limit")

    # Output
    st.divider()
    j_fmt = st.radio("輸出格式", ["MySQL 標準語法", "Tableau 自訂 SQL"], horizontal=True, key="j_fmt")

    if st.button("🔨 產生 SQL", type="primary", key="j_gen"):
        sql = gen_join(j_tables, j_joins, j_col_cfgs, j_conds, j_group_by, j_order_by, j_limit or None)
        if j_fmt == "Tableau 自訂 SQL":
            sql = wrap_tableau(sql)
        st.session_state.j_sql = sql

    if "j_sql" in st.session_state:
        st.markdown("##### ✅ 產生的 SQL（右上角可複製）")
        st.code(st.session_state.j_sql, language="sql")
        if st.button("▶ 執行查詢", key="j_exec"):
            with st.spinner("查詢中..."):
                try:
                    st.session_state.j_df = execute_query(st.session_state.j_sql)
                except Exception as e:
                    st.error(f"查詢失敗：{e}")
        if "j_df" in st.session_state:
            show_query_results(st.session_state.j_df, "j")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — UNION
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.info(
        "💡 使用方式：先在「單表查詢」或「多表 JOIN」頁籤產生 SQL，"
        "點選程式碼區塊右上角的複製按鈕，再貼到下方。"
    )

    u1 = st.text_area("第一段 SQL", height=160, key="u1", placeholder="貼上第一個 SELECT 語句...")

    u_type = st.radio(
        "合併方式", ["UNION", "UNION ALL"],
        horizontal=True,
        help="UNION：自動去除重複資料列 ｜ UNION ALL：保留所有資料（含重複）",
    )

    u2 = st.text_area("第二段 SQL", height=160, key="u2", placeholder="貼上第二個 SELECT 語句...")

    if "show_u3" not in st.session_state:
        st.session_state.show_u3 = False

    col_a, col_b = st.columns([2, 8])
    with col_a:
        if st.button("＋ 新增第三段 SQL"):
            st.session_state.show_u3 = True
    with col_b:
        if st.session_state.show_u3:
            if st.button("移除第三段"):
                st.session_state.show_u3 = False
                st.rerun()

    u3 = ""
    if st.session_state.show_u3:
        u3 = st.text_area("第三段 SQL", height=160, key="u3", placeholder="貼上第三個 SELECT 語句...")

    u_fmt = st.radio("輸出格式", ["MySQL 標準語法", "Tableau 自訂 SQL"], horizontal=True, key="u_fmt")

    if st.button("🔨 產生 UNION SQL", type="primary", key="u_gen"):
        parts = [p.strip() for p in [u1, u2, u3] if p.strip()]
        if len(parts) < 2:
            st.error("請至少輸入兩段 SQL。")
        else:
            sql = f"\n\n{u_type}\n\n".join(parts)
            if u_fmt == "Tableau 自訂 SQL":
                sql = wrap_tableau(sql)
            st.session_state.u_sql = sql

    if "u_sql" in st.session_state:
        st.markdown("##### ✅ 產生的 SQL（右上角可複製）")
        st.code(st.session_state.u_sql, language="sql")
        if st.button("▶ 執行查詢", key="u_exec"):
            with st.spinner("查詢中..."):
                try:
                    st.session_state.u_df = execute_query(st.session_state.u_sql)
                except Exception as e:
                    st.error(f"查詢失敗：{e}")
        if "u_df" in st.session_state:
            show_query_results(st.session_state.u_df, "u")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — 直接執行 SQL
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.markdown("### ▶ 直接執行 SQL")
    st.caption("貼上任何 SELECT 語句，直接查詢資料庫並下載結果。")

    manual_sql = st.text_area(
        "SQL 語句",
        height=200,
        placeholder="SELECT ...\nFROM ...\nWHERE ...",
        key="manual_sql",
    )

    if st.button("▶ 執行", type="primary", key="manual_exec") and manual_sql.strip():
        with st.spinner("查詢中..."):
            try:
                st.session_state.manual_df = execute_query(manual_sql.strip())
            except Exception as e:
                st.error(f"查詢失敗：{e}")

    if "manual_df" in st.session_state:
        show_query_results(st.session_state.manual_df, "manual")
