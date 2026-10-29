"""Calibration tests for `media_stack.sublang`, the lexical subtitle-language scorer.

The ratio ranges come from hand-verification against real library files: genuine
English scores `eng` 0.41-0.49; a genuine French track scored `eng` ~0.03, `fre` ~0.37.
"""

import pytest

from media_stack.sublang import _clean, _is_ass, classify_candidate, score


def _cues(*texts):
    """An SRT with one cue per text (a text may hold several lines)."""
    return "\n".join(
        f"{i}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},900\n{t}\n"
        for i, t in enumerate(texts, 1))


def _body(srt_text):
    return [ln for ln in srt_text.split("\n") if ln and not ln.isdigit() and "-->" not in ln]


ENGLISH_SRT = _cues(
    "I don't know what you want from me tonight.",
    "Just tell the truth, detective. I'm not hiding anything, okay.",
    "She promised she'd arrive early, but nobody noticed her leaving.",
    "Look, this situation feels impossible, but we have to move now.",
    "Okay, let's hurry. There's barely time before the storm hits.",
    "Yeah, I'm listening. What exactly do you need me to do, right.",
    "He promised he would call back, but nobody answered the phone.",
    "Right, so what happens now? We cannot ignore the warning forever.",
    "I know, believe me. Give the engine another minute, okay, before starting.",
    "There you are. I was here the whole time, where were you.",
    "Nothing about this mission makes sense to me anymore, honestly.",
    "Captain, the reactor pressure keeps climbing past safety limits.",
)

FRENCH_SRT = _cues(
    "Je ne sais pas ce que tu attends de moi ce soir.",
    "Dis simplement la vérité, inspecteur. Je ne cache rien.",
    "Elle avait promis d'arriver tôt, personne n'a remarqué son départ.",
    "Écoute, cette situation semble impossible, on doit partir maintenant.",
    "D'accord, dépêchons. Il reste peu de temps avant la tempête.",
    "Oui, j'écoute. Qu'est-ce que tu veux exactement que je fasse.",
    "Il avait promis de rappeler, personne n'a répondu au téléphone.",
    "Bien, alors que se passe-t-il maintenant? On peut ignorer l'avertissement.",
    "Je sais, crois-moi. Laisse le moteur encore une minute avant de démarrer.",
    "Te voilà enfin. Tout le monde se demandait où tu avais disparu.",
    "Rien dans cette mission a du sens honnêtement.",
    "Capitaine, la pression du réacteur dépasse les limites de sécurité, oui.",
)

DANISH_SRT = _cues(
    "Jeg ved ikke hvad du vil have fra mig i aften.",
    "Sig mig sandheden, detektiv. Jeg skjuler ikke noget.",
    "Hun sagde hun ville komme tidligt, men ingen så hende gå.",
    "Hør her, det her er svært for dig, men vi skal gå nu.",
    "Okay, lad os skynde os. Der er ikke meget tid før stormen kommer.",
    "Ja, jeg er her. Hvad vil du have jeg skal gøre, nu.",
    "Han sagde han ville ringe tilbage, men ingen svarede telefonen.",
    "Nej, så hvad nu? Vi kan ikke bare ignorere advarslen for evigt.",
)

CZECH_SRT = _cues(
    "Nevím, co ode mě chceš dnes večer.",
    "Řekni mi pravdu, detektive. Nic neskrývám.",
    "Řekla, že přijde brzy, ale nikdo ji neviděl odcházet.",
    "Poslouchej, tohle je pro tebe těžké, ale musíme jít teď.",
    "Dobře, pospěšme si. Není moc času, než přijde bouře.",
    "Ano, jsem tady. Co chceš, abych udělal, teď hned.",
    "Řekl, že zavolá zpátky, ale nikdo nezvedl telefon.",
    "Ne, tak co teď? Nemůžeme tohle varování ignorovat navždy.",
)

JAPANESE_NONLATIN = _cues(
    "私はあなたが今夜私に何を望んでいるのか分かりません。",
    "本当のことを言ってください、刑事さん。何も隠していません。",
)

SPANISH_SRT = _cues(
    "No sé qué quieres de mí esta noche.",
    "Dime la verdad, detective. No estoy escondiendo nada.",
    "Ella prometió que llegaría temprano, pero nadie la vio salir.",
    "Escucha, esta situación parece imposible, pero debemos irnos ahora.",
    "Bien, apurémonos. Queda poco tiempo antes de la tormenta.",
    "Sí, estoy escuchando. ¿Qué quieres exactamente que haga?",
    "Él prometió llamar de vuelta, pero nadie contestó el teléfono.",
    "No, ¿entonces qué pasa ahora? No podemos ignorar la advertencia para siempre.",
)

CYRILLIC_SRT = _cues(
    "Я не знаю, что ты хочешь от меня сегодня вечером.",
    "Скажи мне правду, детектив. Я ничего не скрываю.",
    "Она обещала прийти рано, но никто не видел, как она уходила.",
)

# Too little text to classify, but genuinely English: must not be rejected.
TWO_CUE_ENGLISH_SRT = _cues("Shh!", "Someone's coming.")

# Finnish has no word list; repeated to clear the 200-token high-confidence
# threshold (204 tokens, eng ratio ~0.015).
_FINNISH_BLOCK = (
    "Minä en tiedä mitä haluat minulta tänä iltana. "
    "Kerro minulle totuus, etsivä. En piilota mitään. "
    "Hän lupasi tulla ajoissa, mutta kukaan ei nähnyt hänen lähtevän. "
    "Kuuntele, tämä tilanne vaikuttaa mahdottomalta, mutta meidän täytyy mennä nyt. "
    "Hyvä, kiirehditään. Aikaa on vähän ennen myrskyä. "
    "Kyllä, minä kuuntelen. Mitä haluat minun tekevän? "
    "Hän lupasi soittaa takaisin, mutta kukaan ei vastannut puheluun. "
    "Ei, mitä nyt tapahtuu? Emme voi jättää varoitusta huomiotta ikuisesti. "
)
FINNISH_HIGH_TOKEN = _FINNISH_BLOCK * 3

# Swahili-ish filler: no overlap with any word list, 40 tokens, well under the 200 floor.
LOW_TOKEN_UNKNOWN = " ".join(
    ["habari", "gani", "rafiki", "yangu", "leo", "ni", "siku", "njema"] * 5)

# 60-token "la la la" caption without music markers: ratio 1.0 for fre/spa/ita
# but ONE distinct word.
LA_LA_LA_CAPTION_SRT = "".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\nLa la la la\n\n"
    for i in range(1, 16))

# >30% non-Latin letters, so score() says NON-LATIN, but the Latin tokens are English.
ENGLISH_WITH_JAPANESE_SIGNS_SRT = _cues(
    "Look out, we have to go now.",
    "It is not safe here, you know.",
    "立入禁止区域につき関係者以外の立ち入りを固く禁じます",
    "非常口はこちらです足元にご注意ください",
    "I am going with you.",
)

# SDH conventions: sound labels, speaker labels, music notes, lyrics.
SDH_SRT = _cues(
    "[music]",
    "JOHN: I don't know what you want from me.",
    "♪ La la la la la la ♪",
    "[door slams]",
    "MARY: Just tell the truth, okay? We have to go.",
    "♪ La la la, la la la ♪",
    "(sighs) She promised she would come, but nobody saw her.",
)

# 19 words with 10 distinct French stopwords, in the shape of a sound label.
FRENCH_LABEL = "le bruit de la porte et les pas dans la rue avec un chien qui est sur le toit"
ENGLISH_LABEL = "the music is playing and the dog is on the roof with the man that you know"

# A French ASS header (Title:, Original Script:, ...): not dialogue; the same text as SRT lines would be.
FRENCH_ASS_META = (
    "Title: Le meilleur film de la vie et ce que nous avons vu dans la rue\n"
    "Original Script: Jean et Marie pour les enfants de la ville avec le chien\n"
    "Original Translation: ce n'est pas une histoire que je veux dans la maison\n"
    "Original Editing: le monde est un lieu que nous avons dans le coeur\n"
    "Update Details: une version pour les amis de la famille et le village\n")


def test_english_sample_scores_eng_in_calibrated_range():
    r = score(ENGLISH_SRT)
    assert r["verdict"] == "eng"
    assert 0.41 <= r["eng"] <= 0.49


def test_french_sample_is_not_english_and_matches_calibration():
    r = score(FRENCH_SRT)
    assert r["verdict"] != "eng"
    assert r["verdict"] == "fre"
    assert r["eng"] < 0.05
    assert 0.3 <= r["best_ratio"] <= 0.45


def test_nonlatin_text_gives_non_latin_verdict():
    r = score(JAPANESE_NONLATIN)
    assert r["verdict"] == "NON-LATIN"


def test_too_little_text_gives_too_little_text_verdict():
    r = score("Hello there. Just a bit of text, not much at all here.")
    assert r["verdict"] == "TOO-LITTLE-TEXT"


def test_empty_text_is_too_little_text_not_a_crash():
    r = score("")
    assert r["verdict"] == "TOO-LITTLE-TEXT"
    assert r["tokens"] == 0
    assert r["nonlatin"] == 0.0


# classify_candidate() -- consolidate-subs.py's content gate.  A tag or filename
# claiming English is a claim, not proof.


def test_classify_candidate_accepts_english():
    is_eng, info = classify_candidate(ENGLISH_SRT)
    assert is_eng is True
    assert info["verdict"] == "eng"
    assert info["reason"] == "eng"


@pytest.mark.parametrize("srt, verdict", [
    (FRENCH_SRT, "fre"), (DANISH_SRT, "dan"), (CZECH_SRT, "cze"), (SPANISH_SRT, "spa")],
    ids=["french", "danish", "czech", "spanish"])
def test_classify_candidate_rejects_listed_foreign_language(srt, verdict):
    is_eng, info = classify_candidate(srt)
    assert is_eng is False
    assert info["verdict"] == verdict


def test_classify_candidate_rejects_cyrillic():
    is_eng, info = classify_candidate(CYRILLIC_SRT)
    assert is_eng is False
    assert info["verdict"] == "NON-LATIN"
    assert info["reason"] == "non-latin"


def test_classify_candidate_accepts_two_cue_english():
    """A 2-cue "Shh!" subtitle is accepted (benefit of the doubt), not rejected for being short."""
    is_eng, info = classify_candidate(TWO_CUE_ENGLISH_SRT)
    assert is_eng is True
    assert info["verdict"] == "TOO-LITTLE-TEXT"
    assert info["reason"] == "too-little-text"


def test_classify_candidate_rejects_high_token_unknown_language():
    """A long unlisted-language sample that barely resembles English is presumptively foreign."""
    is_eng, info = classify_candidate(FINNISH_HIGH_TOKEN)
    assert is_eng is False
    assert info["verdict"] == "UNKNOWN"
    assert info["tokens"] >= 200
    assert info["eng"] < 0.10
    assert info["reason"] == "unknown-language-high-token-low-eng-ratio"


def test_classify_candidate_accepts_low_token_unknown_language():
    """A short unlisted-language sample gets the benefit of the doubt."""
    is_eng, info = classify_candidate(LOW_TOKEN_UNKNOWN)
    assert is_eng is True
    assert info["verdict"] == "UNKNOWN"
    assert info["tokens"] < 200
    assert info["reason"] == "unknown-language-low-token"


# False-rejection regressions: a wrong rejection moves a genuine English sidecar
# out of the library, so a candidate is rejected only on real evidence.

def test_song_caption_of_one_repeated_word_is_not_french():
    """60 tokens of "la" is fre ratio 1.0, but one distinct stopword is not evidence."""
    r = score(LA_LA_LA_CAPTION_SRT)
    assert r["verdict"] == "fre" and r["tokens"] == 60  # why the old rule rejected it
    assert r["best_distinct"] == 1
    is_eng, info = classify_candidate(LA_LA_LA_CAPTION_SRT)
    assert is_eng is True
    assert info["reason"].startswith("weak-evidence-for:")


def test_english_dialogue_with_japanese_signs_is_not_rejected_as_non_latin():
    """English ratio among the Latin tokens is high; the Japanese signs only trip the 30% non-Latin test."""
    r = score(ENGLISH_WITH_JAPANESE_SIGNS_SRT)
    assert r["verdict"] == "NON-LATIN"  # why the old rule rejected it
    assert r["eng"] >= 0.15
    is_eng, info = classify_candidate(ENGLISH_WITH_JAPANESE_SIGNS_SRT)
    assert is_eng is True
    assert info["reason"] == "non-latin-but-substantial-english"


def test_non_latin_with_a_couple_of_stray_english_tokens_still_rejected():
    """The NON-LATIN exemption needs >= 3 English stopword hits."""
    text = JAPANESE_NONLATIN + "\n\n3\n00:00:07,000 --> 00:00:08,000\nI a\n"
    r = score(text)
    assert r["verdict"] == "NON-LATIN" and r["eng"] >= 0.15
    is_eng, info = classify_candidate(text)
    assert is_eng is False
    assert info["reason"] == "non-latin"


def test_utf16_english_is_accepted_once_decoded():
    """UTF-16 English decoded as UTF-8 is NUL-interleaved garbage; decoded properly it is English."""
    from media_stack.subtitles import decode_subtitle_bytes
    for raw in (ENGLISH_SRT.encode("utf-16"),            # LE + BOM
                ENGLISH_SRT.encode("utf-16-le"),         # BOM-less
                ENGLISH_SRT.encode("utf-16-be"),         # BOM-less
                b"\xfe\xff" + ENGLISH_SRT.encode("utf-16-be")):
        assert score(raw.decode("utf-8", errors="replace"))["verdict"] != "eng"
        text = decode_subtitle_bytes(raw)
        assert text == ENGLISH_SRT or text.lstrip("﻿") == ENGLISH_SRT
        assert classify_candidate(text)[0] is True


def test_sdh_conventions_are_accepted():
    is_eng, info = classify_candidate(SDH_SRT)
    assert is_eng is True
    assert info["verdict"] == "eng"


def test_music_and_sound_labels_only_file_is_accepted():
    text = "".join(
        f"{i}\n00:01:{i:02d},000 --> 00:01:{i:02d},900\n[music] ♪ la la la la la ♪\n\n"
        for i in range(1, 16))
    assert classify_candidate(text)[0] is True


# --- SDH labels must not decide the language ---

def test_a_file_of_only_sound_labels_is_too_little_text_and_accepted():
    """15 cues of 19 words each (285 tokens) that, as plain text, are clearly
    French. Inside (...) they are labels, not dialogue: nothing is left to
    judge, so the verdict is TOO-LITTLE-TEXT and the gate accepts the file."""
    plain = _cues(*[FRENCH_LABEL] * 15)
    assert classify_candidate(plain)[0] is False and score(plain)["tokens"] == 285   # the premise
    labels = _cues(*[f"({FRENCH_LABEL})" for _ in range(15)])
    r = score(labels)
    assert r["verdict"] == "TOO-LITTLE-TEXT" and r["tokens"] == 0
    assert classify_candidate(labels) == (True, {**r, "reason": "too-little-text"})
    square = _cues(*[f"[{FRENCH_LABEL}]" for _ in range(15)])
    assert classify_candidate(square)[0] is True and score(square)["tokens"] == 0


def test_labels_mixed_with_english_dialogue_still_score_english():
    english = _body(ENGLISH_SRT)
    plain = _cues(*[f"{FRENCH_LABEL}\n{c}" for c in english])
    assert score(plain)["verdict"] == "fre"                                    # the premise
    mixed = _cues(*[f"[{FRENCH_LABEL}]\n{c}" for c in english])
    assert score(mixed)["verdict"] == "eng"
    assert classify_candidate(mixed)[0] is True
    assert score(mixed)["tokens"] == score(_cues(*english))["tokens"]            # labels add no tokens


def test_labels_mixed_with_french_dialogue_are_still_rejected():
    """English labels must not rescue a French file either."""
    french = _body(FRENCH_SRT)
    plain = _cues(*[f"{ENGLISH_LABEL}\n{c}" for c in french])
    assert classify_candidate(plain)[0] is True                                  # the premise: it hid it
    mixed = _cues(*[f"[{ENGLISH_LABEL}]\n{c}" for c in french])
    assert score(mixed)["verdict"] == "fre"
    assert classify_candidate(mixed)[0] is False


def test_speaker_labels_music_spans_and_brackets_are_removed_from_the_text():
    cases = {
        "JOHN: Hello there": "Hello there",
        "- MARY: Hi": "Hi",
        "DR. SMITH: Fine": "Fine",
        "MAN 1: Yes": "Yes",
        "(sighs) Well": "Well",
        "[door slams] Well": "Well",
        "Well (laughing) then": "Well   then",
        "\u266a La la la \u266a Well": "Well",
        "# Lyric words here # Well": "Well",
        "[JOHN] Well": "Well",
    }
    for raw, wanted in cases.items():
        assert " ".join(_clean(_cues(raw)).split()) == " ".join(wanted.split()), raw


def test_ordinary_colons_and_hash_signs_in_dialogue_are_kept():
    for text in ("Note: this is fine", "It is 10:30 now", "Room 3:30 please", "I am the #1 fan",
                 "John: hello", "Meet at 5:00"):
        assert " ".join(_clean(_cues(text)).split()) == text
    out = _clean(_cues("Press # to continue"))      # a lone # is a music marker, to the end of its cue
    assert "Press" in out and "continue" not in out


def test_a_label_or_music_span_runs_over_the_lines_of_its_cue_only():
    text = _cues("(speaking\nFRENCH) Hello there", "\u266a first line\nsecond line \u266a", "Next cue stays")
    out = _clean(text)
    assert "FRENCH" not in out and "first" not in out and "second" not in out
    assert "Hello there" in out and "Next cue stays" in out
    # a marker that is never closed ends with its cue, and an unclosed bracket swallows nothing
    text = _cues("\u266a lone opening marker", "Kept dialogue one", "(unclosed label", "Kept dialogue two")
    out = _clean(text)
    assert "lone" not in out and "Kept dialogue one" in out
    assert "Kept dialogue two" in out and "unclosed label" in out


def test_ass_dialogue_and_section_headers_are_cleaned_too():
    ass = ("[Script Info]\nTitle: x\n[Events]\nFormat: Layer, Start, End, Style, Name, ML, MR, MV, Effect, Text\n"
           "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\an8}JOHN: Hello there\\N(sighs)\n"
           "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,\u266a la la \u266a Goodbye\n")
    out = " ".join(_clean(ass).split())
    assert "Hello there" in out and "Goodbye" in out
    assert "Script" not in out and "Events" not in out and "sighs" not in out and "la la" not in out


def test_a_raw_ass_file_is_classified_from_its_dialogue_only():
    """The gate sees an extracted ASS track before conversion; only `Dialogue:` text counts."""
    def ass(lines):
        head = ("[Script Info]\nTitle: Some Show\nScriptType: v4.00+\n\n[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,template syl all the la la la\n")
        return head + "".join(
            f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i:02d}.90,Default,,0,0,0,,{{\\an8}}{ln}\n"
            for i, ln in enumerate(lines, 1))

    english = ass(_body(ENGLISH_SRT))
    r = score(english)
    assert r["verdict"] == "eng" and "template" not in _clean(english)
    assert classify_candidate(english)[0] is True
    assert classify_candidate(ass(_body(FRENCH_SRT)))[0] is False


def test_ass_header_metadata_never_counts_only_dialogue_lines_do():
    """A French ASS header (`Title:`, style names) is not dialogue; the same text as SRT lines would be."""
    assert classify_candidate(_cues(*FRENCH_ASS_META.splitlines()))[0] is False     # the premise: French
    ass = ("[Script Info]\n" + FRENCH_ASS_META + "ScriptType: v4.00+\n\n"
           "[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
           "Style: Le_Grand_Style_de_la_Maison,Arial,20\n\n[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           "Comment: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,le chien est dans la rue avec nous\n"
           "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Where is the station?\n"
           "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\\an8}It is over there, I think.\n")
    r = score(ass)
    assert r["verdict"] in ("eng", "TOO-LITTLE-TEXT")
    assert classify_candidate(ass)[0] is True
    assert _clean(ass).split() == ["Where", "is", "the", "station?", "It", "is", "over", "there,", "I", "think."]


def test_a_literal_arrow_inside_ass_dialogue_does_not_make_it_srt():
    """ASS is recognised by structure, not by the absence of '-->'."""
    english = ["You know that I would go there if you want to go with me.",
               "Press A --> B and then we can go home when you are ready.",
               "I do not think that it is a good idea to wait for them here.",
               "Turn the dial --> left, and you can hear what they say to us."]
    header = FRENCH_ASS_META * 3
    ass = ("[Script Info]\n" + header + "ScriptType: v4.00+\n\n[Events]\n"
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           + "".join(f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i:02d}.90,Default,,0,0,0,,{ln}\n"
                     for i, ln in enumerate(english, 1)))
    assert _is_ass(ass.splitlines())
    r = score(ass)
    assert r["verdict"] == "eng" and classify_candidate(ass)[0] is True
    assert " ".join(_clean(ass).split()) == " ".join(english)   # the '-->' lines are kept
    # the premise: read as SRT/plain text, the same header swamps the English
    assert classify_candidate(_cues(*header.splitlines(), *english))[0] is False


def test_ass_is_recognised_by_structure_and_srt_or_vtt_by_cue_timing():
    dialogue = "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello there my friend"
    assert _is_ass(["[Script Info]", "Title: x"])                        # a section header alone
    assert _is_ass(["[Events]"]) and _is_ass([dialogue])                 # or the Dialogue: layout
    assert _is_ass(["Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,a --> b"])
    assert not _is_ass(["Dialogue: not an ASS layout"])                  # a bare prefix proves nothing
    assert not _is_ass(["Hello there", "my friend"])
    # SRT and VTT cue timing lines win over any ASS-looking line in the cue text
    srt = ["1", "00:00:01,000 --> 00:00:02,000", dialogue]
    vtt = ["WEBVTT", "", "00:00:01.000 --> 00:00:02.000", "[Events]"]
    assert not _is_ass(srt) and not _is_ass(vtt)
    assert not _is_ass(["[Script Info]", "00:00:01,000 --> 00:00:02,000"])


def test_an_srt_with_ass_looking_cue_text_is_still_scored_as_srt():
    srt = _cues("Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Je ne sais pas ce que tu veux",
                "[Events]", "Title: le chien est dans la rue avec nous pour la ville et le village")
    # (nothing here is dropped as "metadata": it is all cue text)
    assert "chien" in _clean(srt) and "sais" in _clean(srt)


def test_what_remains_after_the_labels_decides_the_verdict_not_the_labels():
    """20 English label cues (340 tokens as text) around real dialogue: only the
    dialogue counts, so 17 tokens are too little to judge and 34 are English."""
    line = "I do not think that we should go there tonight, but you can if you want to."
    labels = [f"[{ENGLISH_LABEL}]" for _ in range(20)]
    one = score(_cues(*labels, line))
    assert one["verdict"] == "TOO-LITTLE-TEXT" and one["tokens"] == 17
    assert classify_candidate(_cues(*labels, line))[0] is True
    two = score(_cues(*labels, line, line))
    assert two["verdict"] == "eng" and two["tokens"] == 34


def test_foreign_verdict_needs_eight_distinct_stopwords():
    """Exactly the evidence floor: 7 distinct French stopwords (repeated to
    ratio 1.0) is not enough, 8 is."""
    fre8 = "le la les et est un une je".split()
    seven = " ".join(fre8[:7] * 10)
    eight = " ".join(fre8 * 10)
    assert classify_candidate(seven)[0] is True
    assert classify_candidate(eight)[0] is False


def test_foreign_verdict_needs_twice_the_english_ratio():
    """8 distinct French stopwords, but English is a large share too:
    French ratio < 2x the English ratio, so not clearly foreign."""
    fre8 = "le la les et est un une je".split()
    en = "the and you to is it that of a i in what we this be for".split()
    text = " ".join(fre8 * 5 + en * 2)  # 40 French + 32 English tokens
    r = score(text)
    assert r["verdict"] == "fre" and r["best_distinct"] >= 8
    assert r["best_ratio"] < 2 * r["eng"]
    assert classify_candidate(text)[0] is True


def test_czech_sample_has_enough_distinct_stopwords_to_be_rejected():
    """The tokenizer keeps Latin Extended-A letters (ě č ř š ž) whole so "že"/"proč" can match."""
    r = score(CZECH_SRT)
    assert r["best_distinct"] >= 8
    assert classify_candidate(CZECH_SRT)[0] is False
