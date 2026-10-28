# Language samples

Customer-facing name for `pcm` is Broken English. This note says Broken English (Nigerian Pidgin).

Labels: English (`en`), Broken English (`pcm`), Igbo (`ig`), Yoruba (`yo`), Hausa (`ha`).

## Muse Voice

Not probed with audio. The transcribe request this repo sends has no language field:

```json
{"model":"muse-voice-transcribe-1.0","audioEncoding":"WAV","mode":"PUSH_TO_TALK"}
```

It is `model`, `audioEncoding` `WAV`, and `mode` `PUSH_TO_TALK`, plus the wav file. There is no `ig` / `yo` / `ha` / `pcm` switch in that body, and this run did not have `META_API_KEY` or a legally generated speech clip to upload. A tone or silence would not show whether those languages are recognized. Until a short synthetic clip is sent with the key, treat voice notes in Igbo, Yoruba, Hausa, and Broken English as unverified. The transcript, whatever language it comes back in, is then handled in the customer's saved language.

## Live Muse Spark

Not run. `META_API_KEY` was not set in this environment, so no model call was made and nothing below should be read as Muse output. Re-run `python scripts/language_samples.py` on a machine that has the key. The script keeps the spend to one win-back and one price-question reply per language.

## Deterministic mock copy

This is what the offline agent sends when Muse is not called. It is a template, not a native-speaker review. Prices stay in ₦ and the product name is unchanged.

### English (`en`)

Win-back:

Hello Chinedu, this is Mama Nkechi Provisions in Ikeja. Mama Gold Rice 50kg is in stock. We can offer 10% off: ₦70,200 instead of ₦78,000. Month-end is this week, a good time to restock. Reply YES and we will set it aside for you.

Reply to a price question:

Chinedu, Mama Gold Rice 50kg is ₦70,200 right now, and we have 20 in stock. Would you like us to set one aside?

Switch confirmation:

I'll use English from here. Reply LANGUAGE if you want a different one.

Promo overlay: 10% OFF · ₦70,200 · Reply YES on WhatsApp

### Broken English (Nigerian Pidgin) (`pcm`)

Win-back:

Chinedu, how you dey? Na Mama Nkechi Provisions for Ikeja. Mama Gold Rice 50kg still dey. I fit give you 10% off: ₦70,200 instead of ₦78,000. Month-end dey this week, e good time to stock up. Reply YES make we keep am for you.

Reply to a price question:

Chinedu, Mama Gold Rice 50kg na ₦70,200 now, and we get 20 for shop. You wan make we keep one?

Switch confirmation:

I go use Broken English from here. Reply LANGUAGE if you want another one.

Promo overlay: 10% OFF · ₦70,200 · Reply YES on WhatsApp

### Igbo (`ig`)

Win-back:

Chinedu, kedu? Ọ bụ Mama Nkechi Provisions na Ikeja. Mama Gold Rice 50kg ka dị. Enwere m ike inye gị 10% mbelata: ₦70,200 kama ₦78,000. Ọgwụgwụ ọnwa dị n'izu a. Oge ọ dị mma iji zụta ihe. Zaghachi EE ka anyị chekwaara gị ya.

Reply to a price question:

Chinedu, Mama Gold Rice 50kg bụ ₦70,200 ugbu a, anyị nwere 20 n'ụlọ ahịa. Ị chọrọ ka anyị chekwaara gị otu?

Switch confirmation:

Aga m eji Igbo eme n'ihu. Zaa LANGUAGE ma ịchọrọ asụsụ ọzọ.

Promo overlay: 10% mbelata · ₦70,200 · Zaghachi EE na WhatsApp

### Yoruba (`yo`)

Win-back:

Chinedu, báwo ni? Mama Nkechi Provisions ní Ikeja ni èyí. Mama Gold Rice 50kg wà. A lè fún ọ ní 10% ẹdinwo: ₦70,200 dípò ₦78,000. Òpin oṣù wà nínú ọ̀sẹ̀ yìí. Àkókò tó dára láti ra ohun tí a nílò. Dáhùn BẸ́Ẹ̀NI kí a fi pamọ́ fún ọ.

Reply to a price question:

Chinedu, Mama Gold Rice 50kg jẹ́ ₦70,200 báyìí, a sì ní 20 ní ṣọ́ọ̀bù. Ṣé kí a fi ọ̀kan pamọ́ fún ọ?

Switch confirmation:

Màá máa lo Yorùbá láti ìsinsìnyí. Dáhùn LANGUAGE tí o bá fẹ́ èdè mìíràn.

Promo overlay: 10% ẹdinwo · ₦70,200 · Dáhùn BẸ́Ẹ̀NI lórí WhatsApp

### Hausa (`ha`)

Win-back:

Chinedu, sannu? Mama Nkechi Provisions a Ikeja ne. Mama Gold Rice 50kg yana nan. Zan iya ba ka 10% ragiya: ₦70,200 maimakon ₦78,000. Ƙarshen wata yana wannan makon. Lokaci ne mai kyau a sayi kaya. Amsa EE mu ajiye maka ka.

Reply to a price question:

Chinedu, Mama Gold Rice 50kg shi ne ₦70,200 yanzu, muna da 20 a shago. Kana son mu ajiye ɗaya?

Switch confirmation:

Zan yi amfani da Hausa daga yanzu. Amsa LANGUAGE idan kana son wani yare.

Promo overlay: 10% ragiya · ₦70,200 · Amsa EE a WhatsApp

First-contact hint (English, once): Reply LANGUAGE any time to pick English, Broken English, Igbo, Yoruba, or Hausa.

## What to watch

The Igbo, Yoruba, and Hausa lines were drafted as short shop messages, not by a native editor. Tone, diacritics, and word choice may be off. Product names and ₦ amounts are copied from the catalogue on purpose. A reply should stay in one language unless the customer mixes languages first.
