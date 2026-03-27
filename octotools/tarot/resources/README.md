# Tarot Card Images

Card images are not tracked in git. Download them manually from:

https://github.com/MinatoAquaCrews/nonebot_plugin_tarot/tree/master/nonebot_plugin_tarot/resource/BilibiliTarot

Place the files under this directory with the following structure:

```
resources/
├── MajorArcana/   (22 cards: 0-愚者.png ... 21-世界.png)
├── Cups/          (15 files: 圣杯-01.png ... 圣杯骑士.png)
├── Pentacles/     (15 files: 星币-01.png ... 星币骑士.png)
├── Swords/        (15 files: 宝剑-01.png ... 宝剑骑士.png)
└── Wands/         (15 files: 权杖-01.png ... 权杖骑士.png)
```

The tool works without images — `image_path` will be `None` for missing cards.
