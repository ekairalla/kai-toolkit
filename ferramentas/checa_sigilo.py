# -*- coding: utf-8 -*-
r"""Gate de sigilo da KAI Legal Ops: barra commit com dado de cliente, de empregador ou credencial.

Uso:
    python ferramentas/checa_sigilo.py             # confere o que esta staged (usado pelo pre-commit)
    python ferramentas/checa_sigilo.py arquivo...   # confere arquivos avulsos

Nome sigiloso nunca mora no codigo. A lista de termos vem, nesta ordem, de:
    KAI_TERMOS          variavel de ambiente com o caminho do termos_proibidos.json
    git config kai.termos   (use --global: vale para todos os repositorios da KAI)
Sem lista, ou com lista vazia, o gate BARRA o commit: falha fechado, nunca em silencio.
KAI_SIGILOSOS acrescenta nomes extras separados por virgula.
"""
import json
import os
import re
import subprocess
import sys

# e-mail pessoal, ficticio e os enderecos tecnicos que aparecem em commit e remoto (noreply, git@github.com)
DOMINIOS_OK = ("live.com", "gmail.com", "outlook.com", "exemplo.com", "example.com",
               "users.noreply.github.com", "github.com")
SIGILOSOS = [t.strip() for t in os.environ.get("KAI_SIGILOSOS", "").split(",") if t.strip()]

EXT_BLOQUEADA = {".xlsx", ".xlsm", ".csv", ".tsv", ".eml", ".msg", ".pst", ".ost",
                 ".pfx", ".p12", ".pem", ".key", ".zip", ".bak"}
EXT_TEXTO = {".py", ".ps1", ".psm1", ".md", ".txt", ".json", ".yml", ".yaml", ".html", ".htm",
             ".css", ".js", ".ts", ".sql", ".sh", ".bat", ".ini", ".cfg", ".toml", ".xml"}

EMAIL_CORPORATIVO = r"(?i)[\w.\-+]+@(?!%s)[\w.\-]+\.\w{2,}" % "|".join(
    d.replace(".", r"\.") for d in DOMINIOS_OK)

PADROES = [
    ("credencial: chave ou token",
     r"(?i)\b(api[_-]?key|secret|client[_-]?secret|password|senha|passwd|bearer|x-api-key)\b"
     r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-.]{8,}"),
    ("credencial: string de conexao",
     r"(?i)(server|data source)\s*=\s*[^;]+;.*(password|pwd)\s*="),
    ("credencial: token JWT", r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\."),
    ("credencial: chave de provedor",
     r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
     r"|xox[baprs]-[A-Za-z0-9\-]{10,})"),
    ("credencial: SAS do Azure",
     r"(?i)(sig=[A-Za-z0-9%/+=]{20,}|accountkey=[A-Za-z0-9/+=]{20,})"),
    ("numero de processo (CNJ)",
     r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b|\b\d{20}\b"),
    ("CPF", r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    ("CNPJ", r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b"),
    ("e-mail corporativo", EMAIL_CORPORATIVO),
    ("caminho local da maquina", r"(?i)[a-z]:[\\/]+users[\\/]+[A-Za-z0-9_.\-]+"),
    ("caminho de rede interno", r"[\\]{2}[A-Za-z0-9_\-]{3,}[\\][A-Za-z0-9_$\-]+"),
    ("IP privado", r"\b(10\.\d{1,3}|192\.168|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ("recurso interno nomeado",
     r"(?i)\b[a-z0-9\-]+\.(onmicrosoft|sharepoint|azurewebsites|database\.windows)\.(com|net)\b"),
]


def staged():
    saida = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                           capture_output=True, text=True)
    return [l.strip() for l in saida.stdout.splitlines() if l.strip()]


class SemLista(Exception):
    pass


def caminho_termos():
    caminho = os.environ.get("KAI_TERMOS", "").strip()
    if not caminho:
        saida = subprocess.run(["git", "config", "--get", "kai.termos"],
                               capture_output=True, text=True)
        caminho = saida.stdout.strip()
    return caminho


def termos():
    caminho = caminho_termos()
    if not caminho or not os.path.isfile(caminho):
        raise SemLista("lista de termos nao encontrada (KAI_TERMOS ou git config kai.termos): %r"
                       % caminho)
    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)
    if isinstance(dados, dict):
        dados = sum((v for v in dados.values() if isinstance(v, list)), [])
    lista = [str(t) for t in dados if isinstance(t, (str, int)) and len(str(t)) >= 4]
    if not lista:
        raise SemLista("lista de termos vazia: %s" % caminho)
    return SIGILOSOS + lista


def casa_termo(texto, termo):
    borda = r"(?<![0-9A-Za-zÀ-ÿ])%s(?![0-9A-Za-zÀ-ÿ])" % re.escape(termo)
    return re.search(borda, texto, re.IGNORECASE)


def confere(arquivos):
    lista = termos()
    achados = []
    for arq in arquivos:
        ext = os.path.splitext(arq)[1].lower()
        if ext in EXT_BLOQUEADA:
            achados.append((arq, 0, "extensao de dado bloqueada (%s)" % ext, os.path.basename(arq)))
            continue
        if not os.path.isfile(arq) or (ext and ext not in EXT_TEXTO):
            continue
        try:
            with open(arq, encoding="utf-8", errors="replace") as f:
                linhas = f.read().splitlines()
        except OSError:
            continue
        for n, linha in enumerate(linhas, 1):
            for rotulo, padrao in PADROES:
                m = re.search(padrao, linha)
                if m:
                    achados.append((arq, n, rotulo, m.group(0)[:60]))
            for termo in lista:
                if casa_termo(linha, termo):
                    achados.append((arq, n, "termo proibido (cliente ou interno)", termo))
                    break
    return achados


if __name__ == "__main__":
    alvos = sys.argv[1:] or staged()
    if not alvos:
        print("nada para conferir")
        sys.exit(0)
    try:
        problemas = confere(alvos)
    except SemLista as erro:
        print("COMMIT BARRADO: %s" % erro)
        print("Aponte a lista com: git config --global kai.termos <caminho do termos_proibidos.json>")
        sys.exit(2)
    if problemas:
        print("COMMIT BARRADO: %d achado(s) de sigilo\n" % len(problemas))
        for arq, linha, rotulo, trecho in problemas[:40]:
            print("  %s:%s  %s  ->  %s" % (arq, linha or "-", rotulo, trecho))
        if len(problemas) > 40:
            print("  ... e mais %d" % (len(problemas) - 40))
        print("\nTire o dado, ou reescreva com valor ficticio, e faca o commit de novo.")
        sys.exit(1)
    print("OK: %d arquivo(s) conferido(s), nenhum dado sensivel" % len(alvos))
