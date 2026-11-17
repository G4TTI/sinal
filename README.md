# Sinal

<img src="icones/sinal-logo.svg" alt="Sinal" height="64">

(Antes se chamava "Placa de Captura".)

Programa simples para jogar o PS5 (ou ver qualquer webcam/placa de captura) no
computador:

- mostra a imagem da câmera ou placa de captura escolhida;
- pega o som de uma entrada de áudio (o "microfone" da placa, que é o som do
  videogame) e toca na caixa de som que você escolher;
- lembra das escolhas e já abre tudo funcionando na próxima vez;
- ao trocar de câmera, escolhe sozinho o áudio do mesmo aparelho (dá para
  desligar);
- se a placa for desconectada e conectada de novo, volta a funcionar sozinho;
- impede a tela de apagar enquanto o vídeo está aberto (jogando no controle o
  PC não recebe teclado/mouse).

## Instalação

Precisa de Linux com PipeWire (ou PulseAudio), Python 3, GTK 4, libadwaita e
GStreamer. No Arch Linux / Manjaro / CachyOS:

```bash
sudo pacman -S --needed python-gobject gtk4 libadwaita gstreamer gst-plugins-base gst-plugins-good
```

No Fedora:

```bash
sudo dnf install python3-gobject gtk4 libadwaita gstreamer1-plugins-base gstreamer1-plugins-good
```

No Ubuntu / Debian (24.04 ou mais novo):

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-pulseaudio
```

Depois baixe o programa:

```bash
git clone https://github.com/G4TTI/sinal.git
cd sinal
```

## Como usar

```bash
./sinal.py
```

Para colocar no menu de aplicativos do KDE:

```bash
./instalar.sh
```

(`./instalar.sh --remover` tira o atalho. Se você já tinha instalado o
atalho antigo "Placa de Captura", rode o `./instalar.sh` de novo: ele troca
pelo novo, com o ícone do Sinal.)

Atalhos: **F11** ou clique duplo na imagem = tela cheia, **Esc** sai da tela
cheia, **F9** mostra/esconde o painel, **Ctrl+M** silencia, **Ctrl+Q** fecha.

Para ver o que o programa encontrou sem abrir a janela:

```bash
./sinal.py --listar
```

## Dicas para o PS5

- **Sem imagem?** Desative o HDCP no PS5: *Configurações › Sistema › HDMI ›
  Ativar HDCP* → desligado. Com o HDCP ligado a placa não recebe imagem.
- **Barras coloridas** são a tela de "sem sinal" da própria placa: confira o
  cabo HDMI e se o PS5 está ligado.
- A sua placa é uma **MacroSilicon MS2109** (USB 2.0). Ela anuncia 1080p a
  60 fps, mas em 1080p entrega no máximo **30 quadros reais**. Por isso o
  programa usa **1280×720 a 60 fps** por padrão (mais fluido para jogar). Dá
  para trocar em *Resolução e quadros por segundo*; a taxa real aparece no
  topo da janela.
- Se o som falhar ou estalar, aumente o *Buffer do áudio*. Se quiser menos
  atraso, diminua.

## Melhor desempenho (opcional)

Sem nada extra o vídeo já funciona, copiando cada quadro para a tela. Para o
vídeo ser desenhado direto pela placa de vídeo (menos uso de CPU), instale:

```bash
sudo pacman -S gst-plugin-gtk4
```

O programa detecta o plugin sozinho na próxima vez que abrir.

## Como funciona

- Interface: Python + GTK 4 + libadwaita.
- Vídeo: GStreamer `v4l2src → jpegdec → tela`, com fila que descarta quadros
  atrasados para não acumular atraso.
- Áudio: GStreamer `pulsesrc → nível → volume → pulsesink`. As duas pontas
  entram no mesmo grupo do PipeWire (`node.group`), então ele sincroniza o
  relógio da placa com o da caixa de som e o atraso não cresce com o tempo.
- Configurações ficam em `~/.config/sinal/config.json` (na primeira vez ele aproveita as
  configurações antigas de `~/.config/placa-de-captura/`).

## Problemas comuns

| Sintoma | O que fazer |
| --- | --- |
| "está em uso por outro programa" | Feche OBS, navegador, Discord ou outro programa usando a placa e clique em *Tentar de novo*. |
| Sem som | Confira a *Entrada (microfone)*: o medidor *Nível* deve se mexer quando o jogo faz barulho. |
| Eco/microfonia | Você escolheu um microfone de verdade como entrada. Escolha a entrada da placa (ex.: "USB Video"). |
| Som estalando | Aumente o *Buffer do áudio*. |

## Identidade visual

- Ícones em `icones/`: o colorido (`io.github.sinal.Sinal.svg`), o simbólico
  de 16 px e o logo com o nome (`sinal-logo.svg`).
- Cores: azul Sinal `#2F6BFF` (também usado como cor de destaque do app),
  Noite `#0F1526` e Névoa `#F4F5F8` (fundo das telas "sem câmera" e de erro
  nos temas escuro e claro).
- Fontes: Space Grotesk Bold no logotipo e nos títulos, Inter no resto da
  interface. Sem a Space Grotesk instalada, o app usa a Inter no lugar.
- No app: a assinatura (ícone + "sinal") fica no topo da janela e o slogan
  "Seu console na tela do PC. Liga a placa e joga." aparece em *Sobre*.

## Licença

[MIT](LICENSE): pode usar, copiar, modificar e distribuir à vontade.
