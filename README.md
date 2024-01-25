# Yasuho

This bot is a Discord server bot that offers various features, including music playback and fun commands. Enjoy!

You can add the bot to your server using this [invite link](https://invite.yasuho.xyz).

## Features

Yasuho offers a variety of features, including:

- **Music 🎶**: Listen to music with playback controls, pause, skip tracks, and more.

- **Reaction Roles 🎭**: Set up reaction roles to allow users to choose their roles by reacting to messages.

- **And much more!**: Yasuho offers many other features, including fun commands, moderation, and more.

- **Want to see more commands?**: Type **"help (`[category]` | `[command]`)"** to display all commands in a category, see the usage of a specific command, or navigate through the help page.

  - Example with **`[category]`**
  
  - Example with **`[command]`**

## Configuration [WIP]

Before using Yasuho, you need to set up your configuration. Follow these steps:

1. Create a `bot.ini` file based on the provided template.
2. Fill in your bot's specific details, including your bot token and other necessary settings.
3. - Setting Up a PostgreSQL Database: If you plan to use a PostgreSQL database, follow the instructions in the [Yasuho Wiki](https://github.com/yaniswav/Yasuho/wiki/Setting-Up-a-PostgreSQL-Database) to set it up correctly.

Example `bot.ini` file:

```ini
[BotInfo]
ClientID = YOUR_CLIENT_ID
DefaultPrefix = YOUR_DEFAULT_PREFIX
Token = "YOUR_BOT_TOKEN"
...

[Extension]
Extensions = 
    cogs.extension1
    cogs.extension2
    ...
```

3. Create a `tokens.ini` file for any external API tokens.

Example `tokens.ini` file:

```ini
[Website_Tokens]
topGG = "YOUR_TOPGG_TOKEN"
DBL = "YOUR_DBL_TOKEN"
...

[API_Tokens]
nasaKey = "YOUR_NASA_API_KEY"
weatherKey = "YOUR_WEATHER_API_KEY"
...
```

4. Save both files in the `config` directory.

## Built With

* [Discord.py](https://github.com/Rapptz/discord.py)
* [Wavelink](https://github.com/PythonistaGuild/Wavelink/)
* [Lavalink](https://github.com/Frederikam/Lavalink)

## Author

* **yaniswav** - _Development and Hosting_ - [yaniswav](https://github.com/yaniswav)

[![Yasuho on top.gg](https://top.gg/api/widget/498580306773934081.svg)](https://top.gg/bot/498580306773934081)