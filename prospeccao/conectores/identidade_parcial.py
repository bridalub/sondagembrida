"""
Identificação por dados parciais, desacoplada do Google.

A Econodata documenta busca por nome, site, telefone ou e-mail. Sem credencial contratada
o provedor fica desligado e a descoberta segue com Google, site e base local.
Uma identidade sugerida por fornecedor, se um dia houver, continua sujeita à conferência
do programa. Este módulo não chama a rede.
"""
import os


class ProvedorIdentidadeParcial:
    codigo = "econodata"

    def habilitado(self):
        return bool(os.getenv("ECONODATA_API_KEY", "").strip())

    def identificar(self, *, nome="", site="", telefone="", email=""):
        if not self.habilitado():
            return {"habilitado": False, "candidatos": [],
                    "motivo": "provedor de identificação parcial desabilitado; sem credencial"}
        return {"habilitado": False, "candidatos": [],
                "motivo": "credencial presente, integração não contratada nesta versão; nada foi consultado"}
