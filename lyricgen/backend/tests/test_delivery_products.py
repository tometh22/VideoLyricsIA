from delivery_products import product_category


def test_product_category_from_campaign_contract():
    assert product_category("art_track", None) == "artists_art"
    assert product_category("art_track", "hitsdesiempre") == "hits_art"
    assert product_category("art_track", "cancionerorock") == "rock_art"
    assert product_category("lyric_video", "hitsdesiempre") == "hits_lyrics"
    assert product_category("lyric_video", "cancionerorock") == "rock_lyrics"
    assert product_category("lyric_video", None) is None
