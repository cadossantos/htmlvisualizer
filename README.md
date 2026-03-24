# SmartSimple Local PDF Builder

App local em Streamlit para preparar templates HTML antes de subir para o SmartSimple.

## O que faz

- Upload de multiplos arquivos `.html`
- Selecao de 1 documento por vez
- Substituicao de placeholders `@...@`
- Processamento de `SSLOGIC` (`<!--@sslogic(...)--> ... <!--@else--> ... <!--@end-->`)
- Processamento de `sscalculation(...)` para funcoes comuns de contratos
- Preenchimento de `<!--VALOR DOS INSTALMENTS AQUI-->` com base em valor total e numero de parcelas
- Conversao para PDF com PD4ML (opcional)
- Preview local do documento em HTML quando PD4ML nao estiver configurado
- Download do PDF (com PD4ML) ou HTML processado (sem PD4ML)

## Requisitos

- Python 3.11+
- Java (somente para PDF com PD4ML)
- PD4ML jar/licenca local (somente para PDF)

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuracao

Crie um arquivo `.env` na raiz.

Exemplo minimo:

```env
PD4ML_COMMAND_TEMPLATE=java -jar /CAMINHO/pd4ml.jar {input} {output}

SS_DEFAULT_LOGO_URL=[INSERIR LINK]
SS_DEFAULT_SECONDARY_LOGO_URL=[INSERIR LINK]

SS_PARENT_TXT_FSNAME=Global Equality Fund
SS_PARENT_TXT_FSADDRESS=123 Impact Avenue, 10001
SS_PARENT_TXT_FSCOUNTRY=Netherlands
SS_PARENT_TXT_FSPRIMARYCONTACT=Alex Morgan
SS_PARENT_TXT_FSTITLE=Program Director
SS_PARENT_TXT_FSEMAIL=programs@globalequality.org

SS_PARENT_TXT_DDSIGNATORYNAME=Samira Johnson
SS_PARENT_TXT_DDSIGNATORYORGANIZATION=Amnesty International
SS_PARENT_TXT_DDSIGNATORYTITLE=Executive Director
SS_PARENT_TXT_DDSIGNATORYEMAIL=samira.johnson@amnesty.example

SS_PARENT_STARTDATE=2026-01-01
SS_PARENT_ENDDATE=2026-12-31
SS_PARENT_DATE_MIDTERMCHECKIN=2026-07-15
SS_PARENT_DATE_FINALREPORTDUE=2027-01-31
SS_PARENT_TXT_INVITATIONAMOUNT=120000
SS_PARENT_TXT_AMOUNTINWORDS=one hundred twenty thousand US dollars
SS_PARENT_NUMBERINSTALLMENTS=2

SS_LOGO_MAP_GLOBAL_EQUALITY_FUND=[INSERIR LINK]
```

Observacao: o app tambem le `.example.env` (formato legado) para sugerir valores padrao no formulario.
Observacao: se `PD4ML_COMMAND_TEMPLATE` nao estiver configurado, o app entra automaticamente no modo preview HTML.

## Executar

```bash
streamlit run app.py
```

## Escopo de funcoes SS no v1

### Suportado em `sscalculation`

- `date_format(...)`
- `period_diff(...)`
- `timestampdiff(...)` (MONTH, DAY, HOUR)
- `round(...)`
- `format(...)`
- `concat(...)`
- `replace(...)`
- `date(...)`

### Suportado em `SSLOGIC`

- `if`
- `else if`
- `else`
- sem aninhamento

Se uma funcao nao suportada for usada, o app mostra erro explicito para ajuste.

## Notas

- Placeholders sem valor permanecem no documento final (ex.: `@parent.campo@`).
- Quando `@logourl@` nao estiver definido e existir uma imagem local `JIB_AF_Logotipo_Principal (1).png`, o app usa essa imagem local como fallback.
- Segunda logo: o app injeta `@secondarylogourl@` automaticamente ao lado da principal no header quando necessario.
