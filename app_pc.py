import webview
import sys
import os

# O link do teu sistema no Render
URL_SISTEMA = "https://sistem-alfaiataria.onrender.com"


def carregar_app():
    # Cria a janela da app
    webview.create_window(
        title="Alfaiataria Mário Calandula",
        url=URL_SISTEMA,
        width=1200,
        height=800,
        min_size=(800, 600),
        text_select=True  # Permite copiar textos
    )

    # Inicia a app
    # Se quiseres ativar as ferramentas de programador (F12), muda debug=False para True
    webview.start(debug=False)


if __name__ == "__main__":
    carregar_app()
