from __future__ import annotations

import base64
import calendar
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import streamlit as st
from dotenv import dotenv_values


TOKEN_RE = re.compile(r"@[^@\n\r]+@")
SSC_RE = re.compile(r"<!--\s*@sscalculation\((.*?)\)\s*-->", re.IGNORECASE | re.DOTALL)
SSLOGIC_START_RE = re.compile(r"<!--\s*@sslogic\((.*?)\)\s*-->", re.IGNORECASE | re.DOTALL)
END_RE = re.compile(r"<!--\s*@end\s*-->", re.IGNORECASE)
INSTALLMENT_MARKER = "<!--VALOR DOS INSTALMENTS AQUI-->"


DEFAULT_TOKEN_VALUES = {
    "@parent.txt_FSName@": "Global Equality Fund",
    "@parent.txt_FSAddress@": "123 Impact Avenue, 10001",
    "@parent.txt_FSCountry@": "Netherlands",
    "@parent.txt_FSPrimaryContact@": "Alex Morgan",
    "@parent.txt_FSTitle@": "Program Director",
    "@parent.txt_FSEmail@": "programs@globalequality.org",
    "@parent.txt_DDSignatoryName@": "Samira Johnson",
    "@parent.txt_DDSignatoryOrganization@": "Amnesty International",
    "@parent.txt_DDSignatoryTitle@": "Executive Director",
    "@parent.txt_DDSignatoryEmail@": "samira.johnson@amnesty.example",
    "@parent.startdate@": "2026-01-01",
    "@parent.enddate@": "2026-12-31",
    "@parent.selone_InvitationPeriod@": "12 months",
    "@parent.date_FinalReportDue@": "2027-01-31",
    "@parent.date_MidTermCheckIn@": "2026-07-15",
    "@parent.txt_InvitationAmount@": "120000",
    "@parent.txt_AmountInWords@": "one hundred twenty thousand US dollars",
    "@parent.numberinstallments@": "2",
    "@logourl@": "[INSERIR LINK]",
    "@secondarylogourl@": "[INSERIR LINK]",
}


@dataclass
class RenderResult:
    html: str
    values: Dict[str, str]


class RenderError(RuntimeError):
    pass


def to_env_key(token: str) -> str:
    core = token.strip("@").upper()
    core = re.sub(r"[^A-Z0-9]+", "_", core)
    core = re.sub(r"_+", "_", core).strip("_")
    return f"SS_{core}"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return re.sub(r"_+", "_", slug).strip("_")


def parse_money(raw: str) -> Decimal:
    clean = re.sub(r"[^0-9,.-]", "", raw or "")
    if clean.count(",") > 0 and clean.count(".") > 0:
        clean = clean.replace(",", "")
    elif clean.count(",") > 0 and clean.count(".") == 0:
        clean = clean.replace(",", ".")
    if not clean:
        return Decimal("0")
    return Decimal(clean)


def format_usd_amount(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f}"


def compute_installments(total: Decimal, count: int) -> List[Decimal]:
    if count <= 0:
        return []
    base = (total / Decimal(count)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    values = [base for _ in range(count)]
    diff = total - sum(values)
    values[-1] = (values[-1] + diff).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return values


def parse_date_value(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    fmts = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise RenderError(f"Nao foi possivel interpretar data: {value}")


def mysql_date_format(dt: datetime, pattern: str) -> str:
    month_name = calendar.month_name[dt.month]
    marker = "__SS_MONTH_NAME__"
    translated = pattern.replace("%M", marker)
    translated = translated.replace("%i", "%M")
    translated = translated.replace("%h", "%I")
    out = dt.strftime(translated)
    return out.replace(marker, month_name)


def split_args(arg_string: str) -> List[str]:
    args: List[str] = []
    buff: List[str] = []
    depth = 0
    quote: str | None = None

    i = 0
    while i < len(arg_string):
        ch = arg_string[i]
        if quote:
            buff.append(ch)
            if ch == quote and (i == 0 or arg_string[i - 1] != "\\"):
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            buff.append(ch)
            i += 1
            continue

        if ch == "(":
            depth += 1
            buff.append(ch)
            i += 1
            continue

        if ch == ")":
            depth -= 1
            buff.append(ch)
            i += 1
            continue

        if ch == "," and depth == 0:
            args.append("".join(buff).strip())
            buff = []
            i += 1
            continue

        buff.append(ch)
        i += 1

    final = "".join(buff).strip()
    if final:
        args.append(final)
    return args


def resolve_inline_tokens(text: str, values: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.lower().startswith("@date(") and token.endswith("@"):
            inner = token[6:-2]
            source = f"@{inner}@"
            return values.get(source, source)
        return values.get(token, token)

    return TOKEN_RE.sub(repl, text)


def maybe_number(value: object) -> object:
    if isinstance(value, (int, float, Decimal)):
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def eval_sscalc(expr: str, values: Dict[str, str]) -> object:
    expr = expr.strip()

    fn_match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\((.*)\)$", expr, re.DOTALL)
    if fn_match:
        fn = fn_match.group(1).lower()
        raw_args = split_args(fn_match.group(2))
        args = [eval_sscalc(a, values) for a in raw_args]

        if fn == "date_format":
            if len(args) != 2:
                raise RenderError("date_format requer 2 argumentos")
            dt = parse_date_value(args[0])
            fmt = str(args[1])
            return mysql_date_format(dt, fmt)

        if fn == "period_diff":
            if len(args) != 2:
                raise RenderError("period_diff requer 2 argumentos")
            left = str(args[0])
            right = str(args[1])
            if not re.fullmatch(r"\d{6}", left) or not re.fullmatch(r"\d{6}", right):
                raise RenderError("period_diff espera YYYYMM")
            ly, lm = int(left[:4]), int(left[4:])
            ry, rm = int(right[:4]), int(right[4:])
            return (ly - ry) * 12 + (lm - rm)

        if fn == "timestampdiff":
            if len(args) != 3:
                raise RenderError("timestampdiff requer 3 argumentos")
            unit = str(args[0]).strip().upper()
            start = parse_date_value(args[1])
            end = parse_date_value(args[2])
            delta = end - start
            if unit in ("MONTH", "MONTHS"):
                return (end.year - start.year) * 12 + (end.month - start.month)
            if unit in ("DAY", "DAYS"):
                return delta.days
            if unit in ("HOUR", "HOURS"):
                return int(delta.total_seconds() // 3600)
            raise RenderError(f"timestampdiff unit nao suportada: {unit}")

        if fn == "round":
            if len(args) not in (1, 2):
                raise RenderError("round requer 1 ou 2 argumentos")
            number = Decimal(str(args[0]))
            decimals = int(args[1]) if len(args) == 2 else 0
            quant = Decimal(1).scaleb(-decimals)
            return number.quantize(quant, rounding=ROUND_HALF_UP)

        if fn == "format":
            if len(args) < 2:
                raise RenderError("format requer ao menos 2 argumentos")
            number = Decimal(str(args[0]))
            decimals = int(args[1])
            q = number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
            return f"{q:,.{decimals}f}"

        if fn == "concat":
            return "".join(str(a) for a in args)

        if fn == "replace":
            if len(args) != 3:
                raise RenderError("replace requer 3 argumentos")
            return str(args[0]).replace(str(args[1]), str(args[2]))

        if fn == "date":
            if len(args) != 1:
                raise RenderError("date requer 1 argumento")
            dt = parse_date_value(args[0])
            return dt.strftime("%Y-%m-%d")

        if fn == "month":
            if len(args) != 1:
                raise RenderError("month requer 1 argumento")
            dt = parse_date_value(args[0])
            return dt.month

        if fn == "year":
            if len(args) != 1:
                raise RenderError("year requer 1 argumento")
            dt = parse_date_value(args[0])
            return dt.year

        if fn == "day":
            if len(args) != 1:
                raise RenderError("day requer 1 argumento")
            dt = parse_date_value(args[0])
            return dt.day

        raise RenderError(f"Funcao sscalculation nao suportada no v1: {fn}")

    if expr.startswith(("'", '"')) and expr.endswith(("'", '"')) and len(expr) >= 2:
        inner = expr[1:-1]
        return resolve_inline_tokens(inner, values)

    if expr.lower() == "now()":
        return datetime.now()

    if TOKEN_RE.fullmatch(expr):
        return values.get(expr, expr)

    resolved = resolve_inline_tokens(expr, values)
    return maybe_number(resolved)


def eval_sslogic_condition(condition: str, values: Dict[str, str]) -> bool:
    cond = condition.strip()
    fn_match = re.match(r"^(month)\((.*?)\)$", cond, re.IGNORECASE)
    if fn_match:
        arg = eval_sscalc(fn_match.group(2), values)
        dt = parse_date_value(arg)
        return bool(dt.month)

    compare = re.match(r"^(.+?)\s*(=|!=|>=|<=|>|<)\s*(.+)$", cond, re.DOTALL)
    if compare:
        left_raw, op, right_raw = compare.groups()
        left = eval_sscalc(left_raw.strip(), values)
        right = eval_sscalc(right_raw.strip(), values)

        if op == "=":
            return str(left) == str(right)
        if op == "!=":
            return str(left) != str(right)

        try:
            ln = Decimal(str(left))
            rn = Decimal(str(right))
        except Exception:
            ln = str(left)
            rn = str(right)

        if op == ">":
            return ln > rn
        if op == "<":
            return ln < rn
        if op == ">=":
            return ln >= rn
        if op == "<=":
            return ln <= rn

    result = eval_sscalc(cond, values)
    if isinstance(result, str):
        return result.strip().lower() in {"1", "true", "yes"}
    return bool(result)


def process_sslogic(html: str, values: Dict[str, str]) -> str:
    while True:
        start = SSLOGIC_START_RE.search(html)
        if not start:
            return html

        end = END_RE.search(html, start.end())
        if not end:
            raise RenderError("Bloco SSLOGIC sem <!--@end-->")

        cond = start.group(1)
        block = html[start.end(): end.start()]

        branches: List[Tuple[str | None, str]] = []
        cursor = 0
        current_cond: str | None = cond

        marker_re = re.compile(r"<!--\s*@else(?:\s+if\((.*?)\))?\s*-->", re.IGNORECASE | re.DOTALL)
        for marker in marker_re.finditer(block):
            chunk = block[cursor:marker.start()]
            branches.append((current_cond, chunk))
            current_cond = marker.group(1)
            cursor = marker.end()
        branches.append((current_cond, block[cursor:]))

        chosen = ""
        for branch_cond, content in branches:
            if branch_cond is None:
                chosen = content
                break
            if eval_sslogic_condition(branch_cond, values):
                chosen = content
                break

        html = html[:start.start()] + chosen + html[end.end():]


def process_sscalculation(html: str, values: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        expr = match.group(1)
        value = eval_sscalc(expr, values)
        return str(value)

    return SSC_RE.sub(repl, html)


def process_placeholders(html: str, values: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        return values.get(token, token)

    return TOKEN_RE.sub(repl, html)


def extract_placeholders(html: str) -> List[str]:
    found = set(TOKEN_RE.findall(html))
    found.add("@logourl@")
    found.add("@secondarylogourl@")
    return sorted(found)


def parse_legacy_example_env(content: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        token_match = TOKEN_RE.search(line)
        if token_match:
            token = token_match.group(0)
            key = to_env_key(token)
            if key not in mapping and token in DEFAULT_TOKEN_VALUES:
                mapping[key] = DEFAULT_TOKEN_VALUES[token]
    return mapping


def load_config_values(repo_root: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}

    env_path = repo_root / ".env"
    if env_path.exists():
        values.update({k: str(v) for k, v in dotenv_values(env_path).items() if v is not None})

    example_env = repo_root / ".example.env"
    if example_env.exists():
        parsed = parse_legacy_example_env(example_env.read_text(encoding="utf-8"))
        for key, val in parsed.items():
            values.setdefault(key, val)

    values.setdefault("SS_DEFAULT_LOGO_URL", "[INSERIR LINK]")
    values.setdefault("SS_DEFAULT_SECONDARY_LOGO_URL", "[INSERIR LINK]")
    values.setdefault("PD4ML_COMMAND_TEMPLATE", "")
    return values


def resolve_token_values(tokens: Iterable[str], cfg: Dict[str, str], overrides: Dict[str, str], repo_root: Path) -> Dict[str, str]:
    resolved = dict(DEFAULT_TOKEN_VALUES)

    for token in tokens:
        env_key = to_env_key(token)
        if token in cfg:
            resolved[token] = cfg[token]
        elif env_key in cfg:
            resolved[token] = cfg[env_key]

    resolved.update({k: v for k, v in overrides.items() if v != ""})

    logo_path = repo_root / "JIB_AF_Logotipo_Principal (1).png"
    if resolved.get("@logourl@") in (None, "", "[INSERIR LINK]") and logo_path.exists():
        resolved["@logourl@"] = logo_path.resolve().as_uri()

    fs_name = resolved.get("@parent.txt_FSName@", "")
    slug = slugify(fs_name)
    map_key = f"SS_LOGO_MAP_{slug.upper()}" if slug else ""
    secondary = cfg.get(map_key, cfg.get("SS_DEFAULT_SECONDARY_LOGO_URL", "[INSERIR LINK]"))
    if resolved.get("@secondarylogourl@", "") in ("", "[INSERIR LINK]"):
        resolved["@secondarylogourl@"] = secondary

    return resolved


def inject_secondary_logo_if_needed(html: str) -> str:
    if "@secondarylogourl@" in html:
        return html
    logo_tag = '<img class="header_logo" id="header_logo_sm" src="@logourl@"'
    if logo_tag in html:
        insertion = ' <img class="header_logo" id="header_logo_secondary" src="@secondarylogourl@" border="0" alt="Secondary Logo" valign="top" align="left" onerror="this.onerror=null; this.src=\'/images/blank.gif\'"> '
        return html.replace(logo_tag, logo_tag + insertion, 1)
    return html


def apply_installments(html: str, values: Dict[str, str]) -> str:
    if INSTALLMENT_MARKER not in html:
        return html

    amount = parse_money(values.get("@parent.txt_InvitationAmount@", "0"))
    try:
        count = int(str(values.get("@parent.numberinstallments@", "0")))
    except ValueError:
        count = 0

    installments = compute_installments(amount, count)
    if not installments:
        return html

    idx = {"i": 0}

    def repl(_: re.Match[str]) -> str:
        i = idx["i"]
        idx["i"] += 1
        if i < len(installments):
            value = installments[i]
        else:
            value = installments[-1]
        return format_usd_amount(value)

    return re.sub(re.escape(INSTALLMENT_MARKER), repl, html)


def render_html(raw_html: str, values: Dict[str, str]) -> RenderResult:
    html = inject_secondary_logo_if_needed(raw_html)
    html = process_sslogic(html, values)
    html = process_sscalculation(html, values)
    html = apply_installments(html, values)
    html = process_placeholders(html, values)
    return RenderResult(html=html, values=values)


def run_pd4ml(html: str, cfg: Dict[str, str]) -> bytes:
    cmd_template = cfg.get("PD4ML_COMMAND_TEMPLATE", "").strip()

    with tempfile.TemporaryDirectory(prefix="ssdoc_") as td:
        temp_dir = Path(td)
        in_path = temp_dir / "input.html"
        out_path = temp_dir / "output.pdf"
        in_path.write_text(html, encoding="utf-8")

        candidate_cmds: List[List[str]] = []

        if cmd_template:
            candidate_cmds.append(shlex.split(cmd_template.format(input=str(in_path), output=str(out_path))))

        jars = sorted(Path.cwd().glob("pd4ml*.jar"))
        if jars:
            jar = str(jars[0].resolve())
            candidate_cmds.append(["java", "-jar", jar, str(in_path), str(out_path)])
            candidate_cmds.append(["java", "-jar", jar, "-in", str(in_path), "-out", str(out_path)])

        if not candidate_cmds:
            raise RenderError(
                "PD4ML nao configurado. Defina PD4ML_COMMAND_TEMPLATE no .env, por exemplo: "
                "java -jar /caminho/pd4ml.jar {input} {output}"
            )

        last_error = ""
        for cmd in candidate_cmds:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                return out_path.read_bytes()
            last_error = (proc.stderr or proc.stdout or "Falha ao executar PD4ML").strip()

        raise RenderError(f"Erro na conversao PD4ML: {last_error}")


def render_pdf_preview(pdf_bytes: bytes) -> None:
    b64 = base64.b64encode(pdf_bytes).decode("ascii")
    iframe = (
        "<iframe src=\"data:application/pdf;base64,"
        + b64
        + "\" width=\"100%\" height=\"900\" type=\"application/pdf\"></iframe>"
    )
    st.components.v1.html(iframe, height=920)


def render_html_document_preview(html: str) -> None:
    st.components.v1.html(html, height=920, scrolling=True)


def has_pd4ml_available(cfg: Dict[str, str]) -> bool:
    cmd_template = cfg.get("PD4ML_COMMAND_TEMPLATE", "").strip()
    if cmd_template:
        return True
    return bool(sorted(Path.cwd().glob("pd4ml*.jar")))


def main() -> None:
    st.set_page_config(page_title="SmartSimple Local PDF Builder", layout="wide")
    st.title("SmartSimple Local PDF Builder")
    st.caption("Processa templates HTML com variaveis SmartSimple, SSLOGIC e sscalculation para gerar PDF local.")

    repo_root = Path.cwd()
    cfg = load_config_values(repo_root)

    with st.sidebar:
        st.header("Arquivos")
        uploads = st.file_uploader(
            "Suba um ou mais arquivos .html",
            type=["html", "htm"],
            accept_multiple_files=True,
        )

        if not uploads:
            st.info("Suba ao menos um arquivo HTML para comecar.")
            st.stop()

        names = [u.name for u in uploads]
        selected_name = st.selectbox("Documento", options=names)
        selected = next((u for u in uploads if u.name == selected_name), None)
        if selected is None:
            st.warning("Selecione novamente o arquivo HTML.")
            st.stop()

        raw_html = selected.getvalue().decode("utf-8", errors="ignore")
        tokens = extract_placeholders(raw_html)

        st.header("Configuracao")
        pd4ml_template = st.text_input(
            "PD4ML command template",
            value=cfg.get("PD4ML_COMMAND_TEMPLATE", ""),
            help="Use {input} e {output}. Exemplo: java -jar /caminho/pd4ml.jar {input} {output}",
        )
        cfg["PD4ML_COMMAND_TEMPLATE"] = pd4ml_template

        st.subheader("Variaveis")
        overrides: Dict[str, str] = {}
        with st.expander("Editar placeholders", expanded=True):
            defaults = resolve_token_values(tokens, cfg, {}, repo_root)
            for token in tokens:
                label = f"{token} ({to_env_key(token)})"
                value = st.text_input(label, value=defaults.get(token, ""), key=f"token_{token}")
                overrides[token] = value

        pd4ml_enabled = has_pd4ml_available(cfg)
        preview_label = "Visualizar PDF" if pd4ml_enabled else "Visualizar documento"
        preview = st.button(preview_label, type="primary", use_container_width=True)

    values = resolve_token_values(tokens, cfg, overrides, repo_root)

    try:
        rendered = render_html(raw_html, values)
    except RenderError as err:
        st.error(str(err))
        st.stop()

    tab_html, tab_preview = st.tabs(["HTML processado", "Preview"])

    with tab_html:
        st.subheader("HTML processado")
        st.code(rendered.html[:12000], language="html")
        if len(rendered.html) > 12000:
            st.caption("Preview truncado para 12.000 caracteres.")

    with tab_preview:
        pd4ml_enabled = has_pd4ml_available(cfg)
        if pd4ml_enabled:
            st.subheader("PDF")
            if preview:
                try:
                    pdf_bytes = run_pd4ml(rendered.html, cfg)
                    st.session_state["pdf_bytes"] = pdf_bytes
                except RenderError as err:
                    st.error(str(err))

            pdf_bytes = st.session_state.get("pdf_bytes")
            if pdf_bytes:
                render_pdf_preview(pdf_bytes)
                st.download_button(
                    "Baixar PDF",
                    data=pdf_bytes,
                    file_name=Path(selected_name).stem + ".pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            else:
                st.info("Clique em 'Visualizar PDF' para gerar o documento.")
        else:
            st.subheader("Preview (HTML)")
            st.info("PD4ML/Java nao configurado. Exibindo preview HTML local.")
            if preview:
                st.session_state["html_preview"] = rendered.html

            html_preview = st.session_state.get("html_preview", rendered.html)
            render_html_document_preview(html_preview)
            st.download_button(
                "Baixar HTML processado",
                data=html_preview.encode("utf-8"),
                file_name=Path(selected_name).stem + ".rendered.html",
                mime="text/html",
                use_container_width=True,
            )


if __name__ == "__main__":
    main()
