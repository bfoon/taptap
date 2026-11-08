# TapTap update — Fit's wardrobe: 23 skins and masks for your AI character

**Migration: 0118** adds one field, `FitSettings.looks`. No new packages.

This builds on branch `voucher-unstick`. The zip contains everything since master: the voucher fix and "vouchers
first" updates too. If you already applied those, copying them again changes nothing.

## What you get

Open Fit and press the **hanger** button in its header. That opens the wardrobe:

* **A preview stage.** Hover over (or tap) a look and the big robot tries it on before you choose.
* **Pick and it's on.** Fit hops, waves and says its line, for example *"Ho ho ho, Foon! Your gift: routers that
  stay online."* The robot in the corner wears the look on every page.
* **🎲 Surprise me** picks a random look.
* **Just me / Whole team:**
  * Each person dresses their own Fit.
  * The owner (or anyone with Settings) can choose the look for the whole team.
  * Anyone who never picked their own look sees the team's look.
* **📅 Auto follows the calendar.** It is the default, so nobody has to do anything:

  | When | Fit wears |
  |---|---|
  | Ramadan, Eid al-Fitr (Korité) | 🌙 crescent moon, golden lantern, embroidered kufi |
  | Tobaski (Eid al-Adha) | 🐏 embroidered blue grand boubou, tall cap and a little ram |
  | 14–19 February (Independence Day) | 🇬🇲 Gambia flag sash and a waving flag |
  | December | 🎅 Santa |
  | 31 Dec – 2 Jan | 🎉 New Year party hat and confetti |
  | 24 Oct – 1 Nov | 🎃 Pumpkin head |
  | The rest of the year | 🤖 Classic Fit |

  Eid dates are set through 2032. Like the moon sighting, they can be a day off.

### The 23 looks

| Group | Looks |
|---|---|
| Everyday | 🤖 Classic Fit · 🏆 Golden Fit (crown and gold glow) |
| Christmas & New Year | 🎅 Santa (hat, beard, belt, snow) · ⛄ Snowman (top hat, carrot nose, scarf) · 🦌 Reindeer (antlers, glowing red nose, bell) · 🎉 New Year party |
| Halloween masks | 🎃 Pumpkin head · 🧛 Vampire (fangs, collar, flowing cape) · 🧟 Mummy (bandages, green eyes) · 🧙‍♀️ Witch hat and broom · 💀 Skull mask with marigolds — bats fly around these |
| Eid, Tobaski & The Gambia | 🌙 Ramadan & Eid · 🐏 Tobaski boubou · 🇬🇲 Gambia pride |
| Heroes & adventures | 🥷 Ninja (hood, red headband, sword) · 🏴‍☠️ Pirate (tricorn, eye patch, parrot) · 🧑‍🚀 Astronaut (glass helmet, backpack) · 🦸 Superhero (mask, cape, star) · 🧙 Wizard (starry hat, long beard) |
| Animals | 🐱 Cat (ears, whiskers, swinging tail) · 🦁 Lion (full mane) · 🐼 Panda (eye patches, bamboo) · 🐰 Bunny (twitching ears, hearts) |

### How the costumes behave

* **The robot still works the same.** Every costume keeps the eyes visible, so Fit still follows your pointer, blinks,
  thinks, talks, sleeps and hops.
* **Costume parts move:** capes sway, tails swing, the lantern rocks, the flag waves, the parrot nods and the bunny's
  ears twitch.
* **Particles** fit the look: snow, bats, gold sparkles, stars, confetti in the Gambian colours, or hearts. They turn
  off when the phone or computer asks for reduced motion.
* **Fit plays along a little.** It knows what it is wearing and may say one short line in character when greeting
  you. Its answers, numbers and proposed actions never change.
* **In the app:** the hanger button opens the same wardrobe, with emoji tiles and Just me / Whole team. Fit's face in
  the app shows the costume.

## Server

    cd /opt/taptap
    unzip -o ~/taptap-fit-wardrobe.zip -d /tmp && cp -r /tmp/taptap/* /opt/taptap/
    docker compose build web worker vouchers live
    docker compose run --rm web python manage.py migrate core
    docker compose up -d --force-recreate web worker vouchers live

## The app

    cd mobile && flutter pub get && flutter run      # or push: the GitHub workflow builds the APK

## Tests

    python manage.py test core.tests.test_fit_skins

The tests check the calendar, the choices and who may set the team's look. They also check that every look in the
list has a drawing.
