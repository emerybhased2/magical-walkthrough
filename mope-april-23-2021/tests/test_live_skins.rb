# frozen_string_literal: true

# Offline tests for import_live_skins.rb (run: ruby tests/test_live_skins.rb). The client fixture
# is written in the style of the rebuilt 2025 client.js.

require "minitest/autorun"
require_relative "../import_live_skins"

CLIENT = <<~'JS'
  var _0xaaa = 4, _0xbbb = 9, _0xccc = 71, _0xddd = 100, _0xdup = 1, _0xdup = 2;
  var _0x373b4f = function(_0x2c79ac) {
    switch (_0x2c79ac) {
      case _0xbbb:
        _0x276957.aniName = 'Cheetah', _0x276957.aniCol = '#fff', _0x276957.skinName = 'land/bigcat/cheetah';
        break;
      case _0xccc:
        _0x276957.aniName = 'Land Monster', _0x276957.upgradeText = 'x', _0x276957.skinName = 'landmonster';
    }
    return _0x276957;
  };
  _0x74a506.prototype.getAnimalName = function() {
    switch (this.animalType) {
      case _0xaaa:
        return 'fox';
      case _0xdup:
        return 'ambiguous';
    }
  }, _0x74a506.prototype.getSkinPath = function(_0x4ebe1e) {};
  function t(k) { switch (k) { case 'gold_kingDragon':
    return [_0xddd, 1, 'Golden\nKing Dragon', 'volcano/kingdragon/1/kingdragon']; } }
JS

STORE = {
  "catalog" => [
    { "kind" => "SIMPLE", "itemType" => "SKIN", "itemId" => "fox_classic", "forAnimal" => "fox", "basePrice" => 1000, "currency" => "MOPECOINS" },
    { "kind" => "SIMPLE", "itemType" => "SKIN", "itemId" => "cheetah_witch", "forAnimal" => "cheetah", "basePrice" => 600,
      "salePrice" => 480, "currency" => "GEMS" },
    { "kind" => "SIMPLE", "itemType" => "SKIN", "itemId" => "unicorn_x", "forAnimal" => "unicorn", "basePrice" => 5, "currency" => "MOPECOINS" },
    { "kind" => "SIMPLE", "itemType" => "SKIN", "itemId" => "fox_vip", "forAnimal" => "fox", "basePrice" => 5, "currency" => "USD" },
  ],
  "shelves" => [{ "products" => [
    { "kind" => "BUNDLE", "id" => "b", "bundleContents" => [
      { "itemId" => "king_dragon_gold", "itemType" => "SKIN", "forAnimal" => "king_dragon", "basePrice" => 900, "currency" => "MOPECOINS" },
      { "itemId" => "lava_monster_red", "itemType" => "SKIN", "forAnimal" => "lava_monster", "basePrice" => 900, "currency" => "MOPECOINS" },
    ] },
    { "kind" => "SIMPLE", "itemType" => "SKIN", "itemId" => "fox_classic", "forAnimal" => "fox", "basePrice" => 1, "currency" => "MOPECOINS" },
  ] }],
}.freeze

class LiveSkinsTest < Minitest::Test
  # The real client has 100+ animals and the parser refuses fewer than 50; pad the fixture with
  # numeric `case N: return 'name'` entries in getAnimalName().
  def animal_table
    filler = (1..60).map { |n| "case #{300 + n}:\n        return 'filler#{n.digits(26).map { |d| ('a'.ord + d).chr }.join}';\n      " }.join
    CLIENT.sub("case _0xdup:", "#{filler}case _0xdup:")
  end

  def test_animal_table_reads_all_four_sources_and_skips_ambiguous_constants
    t = LiveSkins.animal_types(animal_table)
    assert_equal 4, t["fox"]
    assert_equal 9, t["cheetah"]
    assert_equal 71, t["landmonster"]
    assert_equal 100, t["kingdragon"]
    refute t.key?("ambiguous")
  end

  def test_too_small_table_fails_loudly
    assert_raises(RuntimeError) { LiveSkins.animal_types(CLIENT) }
  end

  def test_live_skins_dedupes_and_reads_bundles_sorted
    ids = LiveSkins.live_skins(STORE).map { |p| p["itemId"] }
    assert_equal %w[cheetah_witch fox_classic fox_vip king_dragon_gold lava_monster_red unicorn_x], ids
    assert_equal 1000, LiveSkins.live_skins(STORE).find { |p| p["itemId"] == "fox_classic" }["basePrice"]
  end

  def test_build_maps_prices_sales_and_skips
    items, skipped = LiveSkins.build(LiveSkins.live_skins(STORE), LiveSkins.animal_types(animal_table))
    by_id = items.to_h { |i| [i[:itemId], i] }
    assert_equal %w[cheetah_witch fox_classic king_dragon_gold lava_monster_red], items.map { |i| i[:itemId] }
    assert_equal [1000, 1001, 1002, 1003], items.map { |i| i[:season] }
    cheetah = by_id["cheetah_witch"]
    assert_equal [9, 1, 600, 2, 480], cheetah.values_at(:animalType, :currencyType, :price, :saleType, :salePrice)
    fox = by_id["fox_classic"]
    assert_equal [4, 0, 0, 0, true], fox.values_at(:animalType, :shopCategory, :currencyType, :saleType, :canBuy)
    assert_equal 71, by_id["lava_monster_red"][:animalType]
    assert_equal 100, by_id["king_dragon_gold"][:animalType]
    reasons = skipped.to_h { |s| [s[:itemId], s[:reason]] }
    assert_match(/not in the 2025 client/, reasons["unicorn_x"])
    assert_match(/currency "USD"/, reasons["fox_vip"])
  end

  def test_seasons_stay_stable_between_runs
    previous = { "fox_classic" => 1000, "lava_monster_red" => 1001 }
    items, = LiveSkins.build(LiveSkins.live_skins(STORE), LiveSkins.animal_types(animal_table), previous)
    seasons = items.to_h { |i| [i[:itemId], i[:season]] }
    assert_equal 1000, seasons["fox_classic"]
    assert_equal 1001, seasons["lava_monster_red"]
    assert_equal [1002, 1003], seasons.values_at("cheetah_witch", "king_dragon_gold").sort
  end

  def test_image_url
    items, = LiveSkins.build(LiveSkins.live_skins(STORE), LiveSkins.animal_types(animal_table))
    assert_equal "https://s3.mope.io/mope-prod/items/king_dragon_gold/king_dragon.ui.webp",
                 LiveSkins.image_url(items.find { |i| i[:itemId] == "king_dragon_gold" })
  end
end
