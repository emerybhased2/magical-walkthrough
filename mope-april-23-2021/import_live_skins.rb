#!/usr/bin/env ruby
# frozen_string_literal: true

# import_live_skins.rb -- put every skin of today's live mope.io shop into the shop of the archived
# 2025-12-10 client (the fetched snapshot in snapshots/2025-12-10).
#
# Sources (public, read-only):
#   https://api.mope.io/store                                        the live shop: catalogue + shelves
#   https://s3.mope.io/mope-prod/items/<itemId>/<forAnimal>.ui.webp  each skin's shop picture
#
# How the archived client shows a shop skin (rebuilt client.js, 2025-12-10 build):
#   * /shop/get items with shopCategory 0 go to the "Skins" tab. The client looks the item's
#     animalType up in its own animal table, then sets skinName = the animal's name and
#     skinImage = the animal's skin path + ".ui" (shop load, `case 0:`).
#   * The tile picture is `assets/s/<item.season>/skins/<skinImage>.png` (getImage/getSkinPath).
#     The client never reads an image URL from the item.
#   * The Skins tab lists only items with canBuy (skinsInterface); its filters read saleType
#     (0 normal, 1 "NEW", 2 "SALE"). The price label uses salePrice only when saleType == 2.
#   * The buy button reads currencyType (0 coins, 1 gems).
# So each live skin gets the 2025 animalType of its animal and a season number of its own
# (SEASON_START and up, kept stable between runs), and the local web gateway answers
# `/assets/s/<that season>/...png` with the skin's picture (manifest.json, SHOP_LIVE_SKINS_DIR).
# The client itself is not changed, and the original archive is never written.
#
# Limits: the tile shows the animal's name (the client overwrites skinName), the season badge shows
# the assigned number, and the skins cannot be worn in a game (the game server knows no such skins).
# Skins of animals the 2025 client does not have, and gem/coin prices the local account server
# cannot charge, are skipped and listed in report.json.
#
# Output (git-ignored, stays on this machine):
#   <snapshot>/live-skins/catalog.json          items, merged into /shop/get by the account server
#   <snapshot>/live-skins/manifest.json         season -> picture, read by the web gateway
#   <snapshot>/live-skins/images/<itemId>.webp  the pictures
#   <snapshot>/live-skins/report.json           what was imported or skipped, and why
#
# Usage:
#   ruby import_live_skins.rb                    # read the shop, download missing pictures, write the files
#   ruby import_live_skins.rb --dry-run          # print the mapping only, download nothing
#   ruby import_live_skins.rb --refresh-images   # download every picture again
#   ruby import_live_skins.rb --store store.json # use a saved copy of api.mope.io/store
# Then restart the account server and web gateway (./start-all.sh restart, or localstack.py).

$stdout.sync = true
require "optparse"
require "json"
require "fileutils"
require "time"
require_relative "live_assets/fetcher"
require_relative "live_assets/formats"

module LiveSkins
  PROJECT = __dir__
  STORE_URL = "https://api.mope.io/store"
  IMAGE_URL = "https://s3.mope.io/mope-prod/items/%<item>s/%<animal>s.ui.webp"
  SEASON_START = 1000
  CURRENCIES = { "MOPECOINS" => 0, "COINS" => 0, "GEMS" => 1 }.freeze
  # live forAnimal (letters only) -> the 2025 client's name, where they differ
  ALIASES = {
    "crocodile" => "croc", "hippopotamus" => "hippo", "rhinoceros" => "rhino", "boa" => "boaconstrictor",
    "orca" => "killerwhale",
    "lavamonster" => "landmonster",   # the volcano monster: "Land Monster" (skins/volcano/landmonster) in 2025
  }.freeze
  CLIENT_MARKER = "_0x74a506.prototype.getAnimalName = function"

  module_function

  def norm(name) = name.to_s.downcase.gsub(/[^a-z]/, "")

  # -- the 2025 client's animal table ---------------------------------------------------------------

  def find_client_js(snapshot)
    candidates = [File.join(snapshot, "working", "client", "mope.io", "client.js"),
                  *Dir[File.join(snapshot, "working", "client-js", "*", "client.js")].sort.reverse]
    found = candidates.find { |path| File.file?(path) && File.read(path, encoding: "UTF-8").include?(CLIENT_MARKER) }
    found or raise "no readable 2025 client.js under #{snapshot}/working; run rebuild_client_js.py first " \
                   "(see phase2/README.md)"
  end

  # Animal name (letters only) -> animalType, from four places in the rebuilt client:
  # the numeric constants, getAnimalName(), animalInfo() (aniName / skinName) and the
  # premium-skin table (`return [<animalType>, <species>, 'Name', 'biome/<animal>/...']`).
  def animal_types(src)
    info_at = src.index("var _0x373b4f = function") or raise "animalInfo() not found in client.js"
    consts = Hash.new { |h, k| h[k] = [] }
    src[0, info_at].scan(/\b(_0x[0-9a-f]+|a_[A-Za-z]+) = (\d+)\b/) { |key, value| consts[key] |= [value.to_i] }
    resolve = lambda do |token|
      return token.to_i if token.match?(/\A\d+\z/)
      values = consts.fetch(token, [])
      values.size == 1 ? values.first : nil
    end
    names = {}
    add = ->(name, token) { (v = resolve.(token)) && names[norm(name)] ||= v }

    start = src.index("_0x74a506.prototype.getAnimalName = function")
    block = src[start...src.index("_0x74a506.prototype.getSkinPath = function", start)]
    block.scan(/case (_0x[0-9a-f]+|\d+):\s*return '([^']+)'/) { |token, name| add.(name, token) }

    block = src[info_at...src.index("return _0x276957", info_at)]
    block.scan(/case (_0x[0-9a-f]+|a_[A-Za-z]+|\d+):\s*(?:case [^:]+:\s*)*_0x276957\.aniName = ('[^']*'|"[^"]*")[^;]*?skinName = '([^']+)'/) do |token, display, skin|
      add.(display[1..-2], token)
      add.(skin.split("/").last, token)
    end

    src.scan(/return \[(_0x[0-9a-f]+), \d+, '[^']*', '[a-z]+\/([a-z]+)\//) { |token, animal| add.(animal, token) }
    raise "only #{names.size} animals found in client.js; is it the rebuilt 2025 client?" if names.size < 50
    names
  end

  # -- the live shop ----------------------------------------------------------------------------------

  # Every SKIN product: the catalogue, the shelves, and the skins inside bundles. => sorted by itemId
  def live_skins(store)
    products = Array(store["catalog"]) + Array(store["shelves"]).flat_map { |s| Array(s["products"]) }
    products += products.flat_map { |p| Array(p["bundleContents"]) }
    skins = {}
    products.each do |p|
      next unless p["itemType"] == "SKIN" && p["itemId"] && p.fetch("kind", "SIMPLE") == "SIMPLE"
      skins[p["itemId"]] ||= p
    end
    skins.keys.sort.map { |k| skins[k] }
  end

  # => [items, skipped]. previous: itemId -> season from the last run, so a skin keeps its season.
  def build(skins, types, previous = {})
    used = previous.values.to_h { |s| [s, true] }
    next_season = [SEASON_START, *previous.values.map { |s| s + 1 }].max
    items = []
    skipped = []
    skins.each do |p|
      animal = p["forAnimal"].to_s
      key = norm(animal)
      animal_type = types[ALIASES.fetch(key, key)]
      currency = CURRENCIES[p["currency"].to_s.upcase]
      reason = if animal_type.nil? then "animal #{animal.inspect} is not in the 2025 client"
               elsif currency.nil? then "currency #{p['currency'].inspect} cannot be bought locally"
               end
      if reason
        skipped << { itemId: p["itemId"], forAnimal: animal, reason: reason }
        next
      end
      season = previous[p["itemId"]]
      unless season
        next_season += 1 while used[next_season]
        season = next_season
        used[season] = true
      end
      price = p["basePrice"].to_i
      item = {
        id: p["itemId"], itemId: p["itemId"], itemType: 0, shopCategory: 0,
        animalType: animal_type, animalSpecies: 0, animalSubSpecies: 0, season: season,
        price: price, currencyType: currency, canBuy: true, saleType: 0,
        live:{ forAnimal: animal, rarity: p["itemRarity"], basePrice: p["basePrice"], currency: p["currency"],
                salePrice: p["salePrice"], discountPercent: p["discountPercent"] },
      }
      sale = p["salePrice"]
      item.merge!(saleType: 2, salePrice: sale) if sale.is_a?(Integer) && sale >= 0 && sale < price
      items << item
    end
    [items.sort_by { |i| i[:season] }, skipped]
  end

  def image_url(item) = format(IMAGE_URL, item: item[:itemId], animal: item[:live][:forAnimal])
end

class LiveSkinImporter
  def initialize(options)
    @o = options
    @log = ->(m) { puts "[#{Time.now.utc.strftime('%H:%M:%S')}] #{m}" }
    @snapshot = File.expand_path(@o[:snapshot], LiveSkins::PROJECT)
    @out = File.expand_path(@o[:out] || File.join(@snapshot, "live-skins"), LiveSkins::PROJECT)
    original = File.join(@snapshot, "archive", "original")
    raise "refusing to write into the original archive" if "#{@out}/".start_with?("#{original}/")
  end

  def rel(path) = path.sub("#{LiveSkins::PROJECT}/", "")

  def store
    if @o[:store]
      @log.call("live shop: #{@o[:store]} (saved copy)")
      return JSON.parse(File.read(@o[:store]))
    end
    @log.call("live shop: #{LiveSkins::STORE_URL}")
    code, type, body, = LiveAssets::Fetcher.new(rate_per_second: 1, log: @log).get(LiveSkins::STORE_URL)
    raise "#{LiveSkins::STORE_URL}: HTTP #{code} (#{type})" unless code == 200
    JSON.parse(body.force_encoding("UTF-8"))
  end

  def previous_seasons
    path = File.join(@out, "catalog.json")
    return {} unless File.file?(path)
    JSON.parse(File.read(path)).fetch("items", []).to_h { |i| [i["itemId"], i["season"].to_i] }
  rescue JSON::ParserError
    {}
  end

  # => nil when the picture is in place, else the reason it is missing
  def download(fetcher, item, images)
    target = File.join(images, "#{item[:itemId]}.webp")
    return nil if !@o[:refresh_images] && File.size?(target) && LiveAssets::Formats.inspect_bytes(File.binread(target))[:ok]
    code, type, body, = fetcher.get(LiveSkins.image_url(item))
    return "HTTP #{code}" unless code == 200
    info = LiveAssets::Formats.inspect_bytes(body)
    return "not a WebP picture (#{info[:format]}, #{type}: #{info[:problems].join('; ')})" unless info[:format] == "webp" && info[:ok]
    File.binwrite("#{target}.part", body)
    File.rename("#{target}.part", target)
    nil
  rescue LiveAssets::Throttled, StandardError => e
    "#{e.class}: #{e.message[0, 120]}"
  end

  def download_all(items)
    images = File.join(@out, "images")
    FileUtils.mkdir_p(images)
    fetcher = LiveAssets::Fetcher.new(rate_per_second: @o[:rate], forbidden_is_final: true, retries: 3, log: @log)
    queue = Queue.new
    items.each { |i| queue << i }
    errors = {}
    lock = Mutex.new
    done = 0
    Array.new([@o[:workers], items.size].min) do
      Thread.new do
        while (item = (queue.pop(true) rescue nil))
          error = download(fetcher, item, images)
          lock.synchronize do
            errors[item[:itemId]] = error if error
            done += 1
            @log.call("pictures: #{done}/#{items.size}") if (done % 50).zero?
          end
        end
        fetcher.close_sessions
      end
    end.each(&:join)
    errors
  end

  def write_json(name, data)
    path = File.join(@out, name)
    File.write("#{path}.part", JSON.pretty_generate(data) + "\n")
    File.rename("#{path}.part", path)
  end

  def run
    client_js = LiveSkins.find_client_js(@snapshot)
    types = LiveSkins.animal_types(File.read(client_js, encoding: "UTF-8"))
    @log.call("2025 client: #{types.values.uniq.size} animal types (#{rel(client_js)})")
    shop = store
    skins = LiveSkins.live_skins(shop)
    items, skipped = LiveSkins.build(skins, types, previous_seasons)
    @log.call("live shop: #{skins.size} skins; #{items.size} map to 2025 animals, #{skipped.size} skipped")
    skipped.each { |s| @log.call("  skip #{s[:itemId].ljust(34)} #{s[:reason]}") }
    if @o[:dry_run]
      items.group_by { |i| i[:live][:forAnimal] }.sort.each do |animal, list|
        puts format("  %-18s animalType %-3d %3d skins  seasons %s", animal, list.first[:animalType], list.size,
                    list.map { |i| i[:season] }.minmax.uniq.join("-"))
      end
      return { items: items, skipped: skipped }
    end

    errors = download_all(items)
    missing = items.select { |i| errors[i[:itemId]] }.map do |i|
      { itemId: i[:itemId], forAnimal: i[:live][:forAnimal], reason: "picture: #{errors[i[:itemId]]}" }
    end
    missing.each { |s| @log.call("  skip #{s[:itemId].ljust(34)} #{s[:reason]}") }
    kept = items.reject { |i| errors[i[:itemId]] }
    source = { store: @o[:store] ? "saved copy of #{LiveSkins::STORE_URL}" : LiveSkins::STORE_URL,
               store_server_time: shop["serverTime"], images: LiveSkins::IMAGE_URL,
               fetched_at: Time.now.utc.iso8601, tool: "import_live_skins.rb" }
    write_json("catalog.json", { _comment: "Generated by import_live_skins.rb; merged into /shop/get by the account server.",
                                 source: source, items: kept })
    write_json("manifest.json", { _comment: "season -> picture, served by the web gateway for /assets/s/<season>/...png",
                                  source: source, seasons: kept.to_h { |i| [i[:season].to_s, "images/#{i[:itemId]}.webp"] } })
    by_currency = kept.group_by { |i| i[:currencyType] == 1 ? "gems" : "coins" }.transform_values(&:size)
    write_json("report.json", { source: source, live_skins: skins.size, imported: kept.size, by_currency: by_currency,
                                on_sale: kept.count { |i| i[:saleType] }, animals: kept.map { |i| i[:live][:forAnimal] }.uniq.size,
                                seasons: kept.map { |i| i[:season] }.minmax, skipped: skipped + missing })
    @log.call("imported #{kept.size}/#{skins.size} skins into #{rel(@out)} (seasons #{kept.map { |i| i[:season] }.minmax.join('-')}); " \
              "restart the account server and web gateway to pick them up")
    { items: kept, skipped: skipped + missing }
  end
end

if $PROGRAM_NAME == __FILE__
  options = { snapshot: "snapshots/2025-12-10", out: nil, store: nil, dry_run: false, refresh_images: false,
              workers: 4, rate: 4.0 }
  OptionParser.new do |o|
    o.banner = "usage: ruby import_live_skins.rb [options]"
    o.on("--snapshot DIR", "the fetched 2025 snapshot (default snapshots/2025-12-10)") { |v| options[:snapshot] = v }
    o.on("--out DIR", "output directory (default <snapshot>/live-skins, = SHOP_LIVE_SKINS_DIR)") { |v| options[:out] = v }
    o.on("--store FILE", "read the shop from a saved api.mope.io/store response") { |v| options[:store] = v }
    o.on("--dry-run", "print the mapping only, download nothing") { options[:dry_run] = true }
    o.on("--refresh-images", "download every picture again") { options[:refresh_images] = true }
    o.on("--workers N", Integer, "parallel downloads (default 4, max 8)") { |v| options[:workers] = v.clamp(1, 8) }
    o.on("--rate N", Float, "picture requests per second (default 4)") { |v| options[:rate] = v.clamp(0.5, 10) }
  end.parse!
  begin
    LiveSkinImporter.new(options).run
  rescue LiveAssets::Throttled, RuntimeError, JSON::ParserError, SystemCallError => e
    warn "error: #{e.message}"
    exit 1
  end
end
