# aft-aroma

Controle por linha de comando para difusores de aroma **AFT** (testado no **AF-11S**), direto do seu computador via Bluetooth Low Energy.

## Motivação

O difusor é controlado por um aplicativo de celular com uma interface desorganizada e complexa: tarefas simples, como ajustar um ciclo de borrifo ou conferir o status, viram uma caça a botões espalhados por várias telas.

Este projeto nasceu de engenharia reversa do protocolo Bluetooth do aparelho: descobrir como o app conversa com o difusor e reimplementar isso num CLI aberto. Com o CLI, a configuração dos difusores fica simples e customizável, um comando direto por ação, e ainda abre espaço pra automatizar e integrar com outros sistemas.

## Objetivos

- Configurar o difusor (ligar, borrifar, ventilador, timers) de forma simples e direta, um comando por ação.
- Documentar o protocolo BLE de forma aberta, pra quem quiser integrar em Home Assistant, scripts, automações, etc.
- Rodar em qualquer lugar: um único arquivo Python, sem instalação complicada.
- Recuperar o acesso ao próprio aparelho quando o PIN for esquecido.

## Requisitos

- Python 3.11 ou superior.
- Bluetooth Low Energy na máquina (embutido na maioria dos notebooks).
- [`uv`](https://docs.astral.sh/uv/) (recomendado) ou `pip`.

No macOS, dê permissão de Bluetooth ao seu terminal em **Ajustes do Sistema → Privacidade e Segurança → Bluetooth**.

## Instalação e execução

Com `uv`, não precisa instalar nada: as dependências estão declaradas dentro do próprio `aft_aroma.py` e o `uv` monta o ambiente sozinho.

```bash
uv run aft_aroma.py scan
```

Com `pip`:

```bash
pip install "bleak>=0.22"
python aft_aroma.py scan
```

## Primeiro uso

1. **Feche o app no celular.** O aparelho aceita uma conexão por vez; se o app estiver conectado, o CLI não acha nada.
2. Encontre o aparelho:

   ```bash
   uv run aft_aroma.py scan
   ```

   ```
   XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX  AF-11S   modelo=2  rssi=-75
   ```

   No macOS o endereço é um UUID gerado pelo CoreBluetooth (é diferente em cada máquina). No Linux/Windows costuma ser o MAC.

3. Crie seu `aroma.toml` a partir do exemplo, com o endereço acima:

   ```bash
   cp aroma.toml.example aroma.toml
   ```

   ```toml
   address = "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
   password = 9999
   ```

   O PIN padrão é **9999**, que parece ser o de fábrica desses aparelhos, e é o valor usado quando você não configura nada. Se o seu difusor recusar (você trocou o PIN no app ou o modelo usa outro de fábrica), ajuste `password` no `aroma.toml` para o PIN que você definiu. Se não lembrar, use o `recover-pin` (veja abaixo).

   Com o `aroma.toml` preenchido, você não precisa passar `-a` nem `-p` nos comandos. O que for passado na linha de comando tem prioridade sobre o arquivo.

## Comandos

Todos assumem que `address` e `password` estão no `aroma.toml`. Senão, use `-a <endereço>` e `-p <PIN>`.

| Comando | O que faz |
|---|---|
| `scan` | Lista os difusores por perto. |
| `status` | Nível de óleo (%), power, ventilador, se está borrifando, janela de trabalho e tempos de ciclo. |
| `spray` | Borrifa agora por ~5 segundos e para sozinho. |
| `power on` / `power off` | Liga ou desliga o difusor. |
| `vent on` / `vent off` | Liga ou desliga o ventilador. |
| `timers` | Lista os ciclos agendados no aparelho. |
| `timer <i> <início> <fim>` | Grava um ciclo agendado (veja abaixo). |
| `rename "<nome>"` | Renomeia o aparelho. |
| `set-password <novo>` | Troca o PIN do aparelho. |
| `reset-oil` | Zera o contador de nível de óleo (após reabastecer). |
| `factory-reset` | Restaura configurações de fábrica. |
| `recover-pin` | Recupera o PIN esquecido do seu próprio aparelho (veja abaixo). |
| `raw <cmd> [payload]` | Envia um comando cru em hex, sem login. Para explorar o protocolo. |

Opções globais: `-a/--address`, `-p/--password`, `-t/--timeout` (tempo de scan), `-v/--verbose` (mostra os bytes trocados), `--listen` (segundos ouvindo respostas após o comando).

### Exemplos

```bash
uv run aft_aroma.py status
uv run aft_aroma.py spray
uv run aft_aroma.py power on
uv run aft_aroma.py -v raw aa 01
```

### Timers

O difusor borrifa por ciclos: liga por X segundos, pausa por Y segundos, dentro de uma janela de horário e nos dias escolhidos. Os tempos são múltiplos de 5 segundos.

```bash
uv run aft_aroma.py timer 1 08:00 22:00 --spray 10 --pause 120 --days 12345
```

Timer 1, das 8h às 22h, borrifando 10s a cada 2 minutos, de segunda a sexta. Dias: `1`=seg ... `7`=dom.

### Recuperar PIN esquecido

O PIN é definido por você no app e **não** vem de fábrica. Se esqueceu o do seu aparelho, o `recover-pin` testa os PINs mais comuns e depois varre a faixa numérica, mantendo uma única conexão e reconectando se cair.

```bash
uv run aft_aroma.py recover-pin           # comuns + 0000-9999 (~25 min)
uv run aft_aroma.py recover-pin --max 99999
```

Isto é uma ferramenta de recuperação para o **dono** do aparelho, no seu próprio equipamento. Não use em aparelho que não é seu.

## Protocolo

Documentado por engenharia reversa; pode variar entre modelos ou versões de firmware.

- **Serviço GATT** `FFF0`. O app escreve os comandos na característica `FFF3` e recebe as respostas por notificação na `FFF4`.
- **Anúncio BLE**: o modelo é identificado por um UUID de serviço no advertisement (`B717`=1, `B817`=2, `B917`=3, `BA17`=4).

**Pacote enviado:**

```
BC <cmd> <len> <payload...> <checksum>
```

`checksum` = soma de todos os bytes anteriores, mantendo só o byte menos significativo (`soma & 0xFF`). A resposta do aparelho começa com `CC` e usa o mesmo checksum.

**Login:** ao conectar, envie `F1` com o PIN em 3 bytes (big-endian). Resposta `CC F1 01 01` = aceito; `CC F1 01 00` = recusado. Sem login aceito, o aparelho não responde aos demais comandos.

| cmd | função | payload |
|---|---|---|
| `01` | sincronizar relógio | ano(2), mês, dia, hora, min, seg, dia-da-semana |
| `02` | gravar timer | índice, inícioH, inícioM, fimH, fimM, borrifo/5, pausa/5, máscara de dias, ativo |
| `03` | ventilador on/off | `01`/`00` |
| `04` | power on/off | `01`/`00` |
| `05` | modo de teste (borrifa ~5s) | `01` |
| `06` | reset de fábrica | `01` |
| `07` | zerar nível de óleo | `01` |
| `AA` | pedir status | `01` |
| `F1` | login | PIN em 3 bytes |
| `F2` | trocar PIN | novo PIN em 3 bytes |
| `F3` | renomear | nome em UTF-8 |

**Status (`CC AA ...`):** óleo% em `[3]`, borrifando em `[4]`, power em `[5]`, ventilador em `[6]`, conectado em `[7]`, janela de trabalho em `[9..12]`, segundos de borrifo em `[13]*5`, segundos de pausa em `[14]*5`.

## Aviso

Projeto independente, sem qualquer vínculo com o fabricante. Use no seu próprio aparelho. `factory-reset`, `set-password` e `recover-pin` alteram ou acessam a configuração do equipamento; use com consciência. O software é fornecido "como está", sem garantias (veja `LICENSE`).

## Licença

MIT. Veja [LICENSE](LICENSE).
