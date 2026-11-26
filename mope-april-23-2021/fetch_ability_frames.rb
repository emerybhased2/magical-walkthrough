#!/usr/bin/env ruby
# frozen_string_literal: true

# fetch_ability_frames.rb -- download every ability image of the live mope.io game: the ability
# frames of every animal (and subspecies) from the live site, and those of every shop skin from
# s3.mope.io, plus the ability effect sprites both serve.
#
# Neither host can be listed, and the site answers unknown paths with its HTML page (HTTP 200), so
# every path comes from the live bundle (read as text, never executed) or the live shop, and only
# real image bytes count. The rules, each read from the bundle:
#
#   animal     the HUD ability icons `assets/animals/<biome>/<animal>/<subspecies>/ability.webp`,
#              `ability2.webp` and the bundle's other ability parts (ability1-6, ability_crabSmash0,
#              ability_pelican, ...) in every animal and subspecies folder (SiteParser)
#   skin       `<s3>/items/<itemId>/ability.webp`, `ability2.webp` for every live shop skin
#              (api.mope.io/store), then the further ability parts its animal turned out to have
#   effect     ability sprites: literal `abilities/...` textures (site, and s3 `items/abilities/...`);
#              `abilities/<ability>/<Er>` for the ability-object enum x each ability class's Er;
#              s3 variants `items/abilities/<ability>/<variant>` for every variant enum (the classes'
#              subspeciesEnum, and the GS/TC/UC-style skin enums of claw/stomp/bite); fireball skins
#              `items/abilities/fireball/<itemId>/{dynamic,static}/0-4`
#   frames     whenever a found file ends in a number (spit0, static/0, ability_crabSmash0, ...) the
#              following numbers are tried until one is missing: animation frames are never guessed
#
# Output (git-ignored): archive/live/abilities/<date>/{mope.io,s3.mope.io}/... (read-only files) and
# manifest.json; a summary is written to abilities/data/report.json (committed).
#
# Usage:
#   ruby fetch_ability_frames.rb                 # everything (~4,000 requests, a few minutes)
#   ruby fetch_ability_frames.rb --plan-only     # print the candidate counts, no image requests
#   ruby fetch_ability_frames.rb --only animal,skin --rate 4 --workers 4
#   ruby fetch_ability_frames.rb --refresh-site  # download the live bundles again first

$stdout.sync = true
require "optparse"
require "json"
require "set"
require "digest"
require "fileutils"
require "time"
require_relative "live_assets/js_scan"
require_relative "live_assets/site_parser"
require_relative "live_assets/formats"
require_relative "live_assets/fetcher"
require_relative "import_live_skins"
require_relative "fetch_live_assets"   # LiveAssets.safe_relative

module AbilityFrames
  PROJECT = __dir__
  SITE = "https://mope.io"
  GROUPS = %w[animal skin effect].freeze
  MAX_FRAMES = 64

  # One path to try. url is absolute; out is the storage path under the output folder.
  Candidate = Struct.new(:url, :out, :group, :owner, :rule, keyword_init: true)

  module_function

  def s3_base(js)
    js.scan(/s3PublicUrl:`(https:\/\/[^`]+)`/).flatten.find { |u| u.include?("prod") }&.chomp("/") or
      raise "s3PublicUrl (prod) not found in the live bundle"
  end

  def ability_parts(parser)
    parts = parser.part_names.grep(/\Aability/)
    (parts + %w[ability ability2]).uniq.sort
  end

  # The enum whose names are the ability-object folders (it holds fire_tornado), and each ability
  # class's [subspeciesEnum, Er].
  def ability_objects(js, enums)
    folders = enums.values.find { |names| names.include?("fire_tornado") } || []
    classes = js.scan(/subspeciesEnum=([\w$]+);Er=`([^`]+)`/).map { |e, er| [enums.fetch(e, []), er] }
    [folders, classes]
  end

  # `${s3}/items/abilities/<ability>/${n}<rest>` where n = <Enum>[...].toLowerCase() a little before.
  def enum_templates(js, enums)
    found = []
    js.to_enum(:scan, /`\$\{\$\.\w+\.s3PublicUrl\}\/items\/abilities\/([a-z_]+)\/\$\{(\w)\}([^`]*)`/).each do
      m = Regexp.last_match
      ability, var, rest = m[1], m[2], m[3]
      before = js[[m.begin(0) - 400, 0].max...m.begin(0)]
      # n=GS[e.Nb[this.equippedItemId]??GS.DEFAULT].toLowerCase(): the index may hold one nested [...]
      enum = before.scan(/\b#{var}=([\w$]+)\[(?:[^\[\]]|\[[^\]]*\])*\]\.toLowerCase\(\)/).flatten.last
      names = enums.fetch(enum, [])
      found << [ability, names, rest] unless names.empty?
    end
    found
  end

  def literal_effects(js)
    js.scan(/`(abilities\/[a-z0-9_\/]+)`/).flatten.uniq.sort
  end

  # Next frame of a numbered file: ".../spit0.webp" -> ".../spit1.webp", ".../static/3.webp" -> 4
  def next_frame(url)
    m = url.match(/\A(.*?)(\d+)(\.webp)\z/) or return nil
    n = m[2].to_i + 1
    n >= MAX_FRAMES ? nil : "#{m[1]}#{n}#{m[3]}"
  end

  def storage_path(url)
    host, path = url.sub(%r{\Ahttps://}, "").split("/", 2)
    File.join(host, LiveAssets.safe_relative(path.to_s))
  end

  def candidate(url, group, owner, rule)
    Candidate.new(url: url, out: storage_path(url), group: group, owner: owner, rule: rule)
  end

  # Phase 1: everything that does not depend on another result.
  def plan(parser, js, skins, only)
    s3 = s3_base(js)
    parts = ability_parts(parser)
    list = []
    if only.include?("animal")
      parser.animal_folders.each do |animal, sub, folder|
        parts.each { |p| list << candidate("#{SITE}/#{folder}#{p}.webp", "animal", "#{animal.name}/#{sub.downcase}", "animal part #{p}") }
      end
    end
    if only.include?("skin")
      skins.each do |s|
        %w[ability ability2].each { |p| list << candidate("#{s3}/items/#{s['itemId']}/#{p}.webp", "skin", s["itemId"], "skin HUD #{p}") }
      end
    end
    if only.include?("effect")
      enums = parser.enums
      literal_effects(js).each do |path|
        list << candidate("#{SITE}/assets/#{path}.webp", "effect", path.split("/")[1], "literal (site)")
        list << candidate("#{s3}/items/#{path}.webp", "effect", path.split("/")[1], "literal (s3)")
      end
      folders, classes = ability_objects(js, enums)
      folders.each do |f|
        classes.each do |names, er|
          list << candidate("#{SITE}/assets/abilities/#{f}/#{er}.webp", "effect", f, "ability object Er")
          (names - ["DEFAULT"]).each do |v|
            list << candidate("#{s3}/items/abilities/#{f}/#{v.downcase}.webp", "effect", f, "ability object variant")
          end
        end
      end
      enum_templates(js, enums).each do |ability, names, rest|
        (names - ["DEFAULT"]).each do |v|
          list << candidate("#{s3}/items/abilities/#{ability}/#{v.downcase}#{rest}.webp", "effect", ability, "skin enum variant")
        end
      end
      skins.each do |s|
        list << candidate("#{s3}/items/abilities/fireball/#{s['itemId']}/dynamic/0.webp", "effect", "fireball", "fireball skin")
      end
    end
    list.uniq(&:url)
  end

  # Phase 2: the further ability parts of a skin's animal; fireball skins' other frame set.
  def follow_up(found, skins, parser, s3)
    animal_parts = Hash.new { |h, k| h[k] = Set.new }
    found.each do |c|
      next unless c.group == "animal" && c.owner.end_with?("/default")
      animal_parts[c.owner.split("/").first] << File.basename(c.url, ".webp")
    end
    list = []
    skins.each do |s|
      (animal_parts[s["forAnimal"]].to_a - %w[ability ability2]).sort.each do |p|
        list << candidate("#{s3}/items/#{s['itemId']}/#{p}.webp", "skin", s["itemId"], "skin part #{p} (its animal has it)")
      end
    end
    found.select { |c| c.url.include?("/items/abilities/fireball/") && c.url.end_with?("/dynamic/0.webp") }.each do |c|
      list << candidate(c.url.sub("/dynamic/0.webp", "/static/0.webp"), "effect", "fireball", "fireball skin")
    end
    list
  end
end

class AbilityFrameFetcher
  def initialize(options)
    @o = options
    @log = ->(m) { puts "[#{Time.now.utc.strftime('%H:%M:%S')}] #{m}" }
    @date = Time.now.utc.strftime("%Y-%m-%d")
    @out = File.expand_path(@o[:output] || File.join("archive", "live", "abilities", @date), AbilityFrames::PROJECT)
    @site = LiveAssets::Fetcher.new(rate_per_second: @o[:rate], log: @log)
    @s3 = LiveAssets::Fetcher.new(rate_per_second: @o[:rate], forbidden_is_final: true, retries: 2, log: @log)
    @lock = Mutex.new
    @requests = 0
    # URLs already answered "missing" by an earlier run (retried with --refresh)
    @misses_file = File.join(@out, "misses.txt")
    @misses = !@o[:refresh] && File.file?(@misses_file) ? File.readlines(@misses_file, chomp: true).to_set : Set.new
    @new_misses = []
  end

  # The live bundles: the newest snapshot of fetch_live_assets.rb, or downloaded now.
  def bundles
    dirs = Dir[File.join(AbilityFrames::PROJECT, "archive", "live", "*", "site")].sort
    if dirs.empty? || @o[:refresh_site]
      @log.call("live bundles: downloading with fetch_live_assets.rb --plan-only")
      ok = system(RbConfig.ruby, File.join(AbilityFrames::PROJECT, "fetch_live_assets.rb"), "--plan-only", out: File::NULL)
      raise "fetch_live_assets.rb --plan-only failed" unless ok
      dirs = Dir[File.join(AbilityFrames::PROJECT, "archive", "live", "*", "site")].sort
    end
    @log.call("live bundles: #{dirs.last.sub("#{AbilityFrames::PROJECT}/", '')}")
    Dir[File.join(dirs.last, "**", "*.{js,css}")].to_h { |f| [f.sub("#{dirs.last}/", ""), File.read(f, encoding: "UTF-8").scrub] }
  end

  def skins
    code, _, body, = LiveAssets::Fetcher.new(rate_per_second: 1, log: @log).get(LiveSkins::STORE_URL)
    raise "#{LiveSkins::STORE_URL}: HTTP #{code}" unless code == 200
    LiveSkins.live_skins(JSON.parse(body.force_encoding("UTF-8")))
  end

  # => [found candidates, missing count]; numbered files pull in their next frame.
  def fetch(list)
    queue = Queue.new
    list.each { |c| queue << c }
    found = []
    missing = 0
    pending = list.size
    done = Queue.new
    workers = Array.new(@o[:workers]) do
      Thread.new do
        loop do
          c = queue.pop
          break if c == :stop
          hit = fetch_one(c)
          nxt = hit && (url = AbilityFrames.next_frame(c.url)) &&
                AbilityFrames.candidate(url, c.group, c.owner, "next frame of #{File.basename(c.url)}")
          @lock.synchronize do
            hit ? found << c : missing += 1
            pending += 1 if nxt
            pending -= 1
            @log.call("  #{found.size} found, #{missing} missing, #{pending} to go") if ((found.size + missing) % 250).zero?
            queue << nxt if nxt
            done << true if pending.zero?
          end
        end
        [@site, @s3].each(&:close_sessions)
      end
    end
    done.pop unless list.empty?
    workers.size.times { queue << :stop }
    workers.each(&:join)
    [found, missing]
  end

  def fetch_one(c)
    path = File.join(@out, c.out)
    if File.size?(path) && !@o[:refresh]
      return LiveAssets::Formats.inspect_bytes(File.binread(path))[:ok]
    end
    return false if @misses.include?(c.url)
    fetcher = c.url.start_with?(AbilityFrames::SITE) ? @site : @s3
    @lock.synchronize { @requests += 1 }
    code, _, body, = fetcher.get(c.url)
    info = code == 200 ? LiveAssets::Formats.inspect_bytes(body) : {}
    unless info[:ok] && %w[webp png jpeg gif].include?(info[:format])
      @lock.synchronize { @new_misses << c.url } if code == 200 || code == 403 || code == 404
      return false
    end
    FileUtils.mkdir_p(File.dirname(path))
    File.chmod(0o644, path) if File.exist?(path)
    File.binwrite(path, body)
    File.chmod(0o444, path)
    true
  rescue LiveAssets::Throttled
    raise
  rescue StandardError => e
    @log.call("  #{c.url}: #{e.class}")
    false
  end

  def describe(c)
    path = File.join(@out, c.out)
    info = LiveAssets::Formats.inspect_bytes(File.binread(path))
    { group: c.group, owner: c.owner, rule: c.rule, url: c.url, file: c.out, format: info[:format],
      width: info[:width], height: info[:height], bytes: File.size(path), sha256: Digest::SHA256.file(path).hexdigest }
  end

  def run
    sources = bundles
    parser = LiveAssets::SiteParser.new(sources)
    js = sources.select { |k, _| k.end_with?(".js") }.values.join("\n")
    shop = skins
    list = AbilityFrames.plan(parser, js, shop, @o[:only])
    counts = list.group_by(&:group).transform_values(&:size)
    @log.call("plan: #{list.size} paths #{counts} (#{parser.animal_folders.size} animal folders, #{shop.size} shop skins, " \
              "ability parts #{AbilityFrames.ability_parts(parser).join(' ')})")
    return { plan: list.size, by_group: counts } if @o[:plan_only]

    found, missing = fetch(list)
    extra = AbilityFrames.follow_up(found, shop, parser, AbilityFrames.s3_base(js)).reject { |c| found.any? { |f| f.url == c.url } }
    @log.call("phase 2: #{extra.size} more paths (skin parts their animal has, fireball static frames)")
    more, missing2 = fetch(extra)
    found = (found + more).uniq(&:url).sort_by(&:url)
    files = found.map { |c| describe(c) }
    by_group = files.group_by { |f| f[:group] }
    summary = {
      generated: Time.now.utc.iso8601, tool: "fetch_ability_frames.rb", site: AbilityFrames::SITE,
      s3: AbilityFrames.s3_base(js), store: LiveSkins::STORE_URL, requests: @requests,
      checked: files.size + missing + missing2,
      missing: missing + missing2, found: files.size, bytes: files.sum { |f| f[:bytes] },
      by_group: by_group.transform_values(&:size),
      animals_with_frames: by_group.fetch("animal", []).map { |f| f[:owner].split("/").first }.uniq.size,
      animal_folders_with_frames: by_group.fetch("animal", []).map { |f| f[:owner] }.uniq.size,
      skins_with_frames: by_group.fetch("skin", []).map { |f| f[:owner] }.uniq.size, shop_skins: shop.size,
      effects: by_group.fetch("effect", []).group_by { |f| f[:owner] }.transform_values(&:size).sort.to_h,
    }
    FileUtils.mkdir_p(@out)
    File.write(@misses_file, (@misses.to_a + @new_misses).uniq.sort.join("\n") + "\n")
    full = (AbilityFrames::GROUPS - @o[:only]).empty?
    name = full ? "manifest.json" : "manifest-#{@o[:only].sort.join('-')}.json"   # a partial run keeps the full one
    File.write(File.join(@out, name), JSON.pretty_generate(summary.merge(files: files)))
    if full
      report = File.join(AbilityFrames::PROJECT, "abilities", "data", "report.json")
      FileUtils.mkdir_p(File.dirname(report))
      File.write(report, JSON.pretty_generate(summary))
    end
    @log.call("found #{files.size} ability images (#{(summary[:bytes] / 1e6).round(1)} MB) #{summary[:by_group]}: " \
              "#{summary[:animal_folders_with_frames]} animal folders, #{summary[:skins_with_frames]}/#{shop.size} skins; " \
              "#{@requests} requests -> #{@out.sub("#{AbilityFrames::PROJECT}/", '')}")
    summary
  end
end

if $PROGRAM_NAME == __FILE__
  options = { only: AbilityFrames::GROUPS, rate: 6.0, workers: 6, output: nil, plan_only: false, refresh: false, refresh_site: false }
  OptionParser.new do |o|
    o.banner = "usage: ruby fetch_ability_frames.rb [options]"
    o.on("--only LIST", Array, "groups: #{AbilityFrames::GROUPS.join(',')} (default all)") do |v|
      bad = v - AbilityFrames::GROUPS
      raise OptionParser::InvalidArgument, bad.join(",") unless bad.empty?
      options[:only] = v
    end
    o.on("--rate N", Float, "requests per second per host (default 6, 0 = no cap)") { |v| options[:rate] = v.clamp(0, 50) }
    o.on("--workers N", Integer, "parallel requests (default 6, max 16)") { |v| options[:workers] = v.clamp(1, 16) }
    o.on("--output DIR", "default archive/live/abilities/<date>") { |v| options[:output] = v }
    o.on("--plan-only", "print the candidate counts only") { options[:plan_only] = true }
    o.on("--refresh", "download found files again and retry known misses") { options[:refresh] = true }
    o.on("--refresh-site", "download the live bundles again first") { options[:refresh_site] = true }
  end.parse!
  begin
    AbilityFrameFetcher.new(options).run
  rescue LiveAssets::Throttled, RuntimeError, SystemCallError => e
    warn "error: #{e.message}"
    exit 1
  end
end
