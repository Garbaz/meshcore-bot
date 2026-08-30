# meshcore-bot

A MeshCore companion bot that responds to commands sent via DMs and channel mentions.

## Deploy

```sh
uv run meshcore-bot -s /dev/ttyUSB0 \
  --location "Mariana Trench" \
  --channels "ping[ping], test[ping,path], weather[weather], bot"
```

See for all options:

```sh
uv run meshcore-bot --help
```

## Usage

DM or @mention the bot in one of the enabled channels. E.g. if the bot is called `🤖 My-Bot`, mention it in a channel with `@[🤖 my-bot]: help` or just `@my-bot help` for a list of the available commands. Available commands can differ between channels. Some commands are DM only. Some commands are "secret" and never listed in `help` even if available in a channel.

## License

MIT
