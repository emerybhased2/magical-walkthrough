#!/usr/bin/env ruby
# frozen_string_literal: true

# fetch_pumpkins.rb -- fetch every pumpkin variant of the live mope.io site (and whatever s3.mope.io
# serves), add the custom variants in custom_assets/pumpkins/, and install them all into the 2025
# working snapshot, where the archived client looks for them:
#
#   ./assets/s/<season>/img/pumpkin/<pumpkinType>/<frame>.png     (client L24937-24939: 5 frames)
#
# 1. variants   read from the live bundle's pumpkin class (`terrain/pumpkin/${[...][subspecies]}`
#               and its frame loop), never assumed; bundles come from the newest live snapshot in
#               archive/live/*/site (or are downloaded with --refresh-site)
# 2. fetch      https://mope.io/assets/terrain/pumpkin/<variant>/<frame>.webp; the bundle's s3 bucket
#               (s3PublicUrl) is probed with the same relative paths and used for any frame the site
#               lacks. The bucket refuses listing, so only those paths can be tried.
# 3. install    WebP -> PNG (ImageMagick) into the working snapshot; pumpkinType = the variant's index
#               in the live list. Custom variants follow: their black background is made transparent
#               (flood fill from the corners, so dark parts inside the outline stay), they are fitted
#               to the live frame size, and 5 frames are made (a 72-degree spin per frame, or --custom-frames still)
# 4. manifest   pumpkins.json next to the images + pumpkins/data/manifest.json (committed)
#
# The original archive (archive/original) is never written. Downloads stay in archive/live/pumpkins/.
#
# Usage: ruby fetch_pumpkins.rb [--dry-run] [--refresh-site] [--custom-frames spin|still] [--workers N]

$stdout.sync = true
require "optparse"
require "json"
require "digest"
require "fileutils"
require "open3"
require "time"
require_relative "live_assets/site_parser"
require_relative "live_assets/formats"
require_relative "live_assets/fetcher"

module Pumpkins
  PROJECT = __dir__
  CUSTOM = %w[amber sapphire amethyst black_hole].freeze   # appended after the live variants, in this order

  Plan = Struct.new(:folder, :variants, :frames, :s3_base, keyword_init: true)

  module_function

  # The live bundle's pumpkin class: `terrain/pumpkin/${[`normal`,...][this.subspecies]}` and
  # `for(let n=0;n<5;n++)t.push(`${e}/${n}`)`.
  def plan_from(js)
    m = js.match(/`(terrain\/pumpkin)\/\$\{\[((?:`[a-z0-9_]+`,?)+)\]\[this\.subspecies\]\}`(.{0,200})/m)
    raise "pumpkin class not found in the live bundle" unless m
    frames = m[3][/for\(let \w=0;\w<(\d+);\w\+\+\)/, 1]
    raise "pumpkin frame loop not found" unless frames
    s3 = js.scan(/s3PublicUrl:`(https:\/\/[^`]+)`/).flatten.find { |u| u.include?("prod") } ||
         js[/s3PublicUrl:`(https:\/\/[^`]+)`/, 1]
    Plan.new(folder: m[1], variants: m[2].scan(/`([a-z0-9_]+)`/).flatten, frames: frames.to_i, s3_base: s3&.chomp("/"))
  end

  def stack_config
    JSON.parse(File.read(File.join(PROJECT, "config", "local_stack.json")))
  end

  def run!(cmd)
    out, status = Open3.capture2e(*cmd)
    raise "#{cmd.first} failed: #{out.strip[0, 300]}" unless status.success?
    out
  end

  def identify(path)
    run!(["identify", "-format", "%w %h %[opaque]", path]).split.then { |w, h, o| [w.to_i, h.to_i, o == "True"] }
  end

  def alpha_at(path, x, y)
    run!(["convert", path, "-format", "%[fx:p{#{x},#{y}}.a]", "info:"]).to_f
  end
end

class PumpkinInstaller
  def initialize(options)
    @o = options
    @log = ->(m) { puts "[#{Time.now.utc.strftime('%H:%M:%S')}] #{m}" }
    config = Pumpkins.stack_config
    @season = (config.dig("game", "season") || 1).to_i
    @client_root = File.expand_path(config.fetch("client_root"), Pumpkins::PROJECT)
    @target = File.join(@client_root, "assets", "s", @season.to_s, "img", "pumpkin")
    @downloads = File.join(Pumpkins::PROJECT, "archive", "live", "pumpkins", Time.now.utc.strftime("%Y-%m-%d"))
    @site = "https://mope.io"
    @live = LiveAssets::Fetcher.new(rate_per_second: 0, log: @log)
    @s3 = LiveAssets::Fetcher.new(rate_per_second: 0, forbidden_is_final: true, retries: 2, log: @log)
    original = File.join(Pumpkins::PROJECT, "archive", "original")
    raise "refusing to write into the original archive" if @target.start_with?(original)
  end

  def bundles
    site_dirs = Dir[File.join(Pumpkins::PROJECT, "archive", "live", "*", "site")].reject { |d| d.include?("/pumpkins/") }.sort
    if site_dirs.any? && !@o[:refresh_site]
      @log.call("live bundles: #{site_dirs.last.sub(Pumpkins::PROJECT + '/', '')}")
      return Dir[File.join(site_dirs.last, "**", "*.js")].map { |f| File.read(f, encoding: "UTF-8") }.join("\n")
    end
    @log.call("live bundles: downloading from #{@site}")
    status, _, html, = @live.get("#{@site}/")
    raise "homepage HTTP #{status}" unless status == 200
    queue = html.scan(/(?:src|href)="\.?\/?([^"]+\.js)"/).flatten.uniq
    seen = {}
    texts = []
    until queue.empty? || seen.size > 400
      rel = queue.shift
      next if seen[rel] || rel.include?("..")
      seen[rel] = true
      code, _, body, = @live.get("#{@site}/#{rel}")
      next unless code == 200
      text = body.dup.force_encoding("UTF-8").scrub
      texts << text
      LiveAssets::SiteParser.chunk_references(text).each do |ref|
        queue << File.expand_path(ref, "/#{File.dirname(rel)}").sub(%r{\A/}, "")
      end
    end
    texts.join("\n")
  end

  def fetch_image(fetcher, url)
    code, type, body, = fetcher.get(url)
    return [code, nil, "HTTP #{code}"] unless code == 200
    info = LiveAssets::Formats.inspect_bytes(body)
    return [code, nil, "#{info[:format]} (#{type})"] unless info[:ok] && info[:format] != "html"
    [code, body, info]
  end

  def save(path, bytes)
    FileUtils.mkdir_p(File.dirname(path))
    File.chmod(0o644, path) if File.exist?(path)
    File.binwrite(path, bytes)
    File.chmod(0o444, path)
  end

  # -- 2. fetch ------------------------------------------------------------------------------------
  def fetch_live(plan)
    jobs = plan.variants.flat_map { |v| (0...plan.frames).map { |n| [v, n] } }
    results = {}
    lock = Mutex.new
    queue = Queue.new
    jobs.each { |j| queue << j }
    Array.new([@o[:workers], jobs.size].min) do
      Thread.new do
        while (job = (queue.pop(true) rescue nil))
          v, n = job
          rel = "#{plan.folder}/#{v}/#{n}.webp"
          code, body, info = fetch_image(@live, "#{@site}/assets/#{rel}")
          if body
            path = File.join(@downloads, "mope.io", "assets", rel)
            save(path, body)
            lock.synchronize { results[[v, n]] = { source: "#{@site}/assets/#{rel}", file: path, sha256: Digest::SHA256.hexdigest(body) } }
          else
            lock.synchronize { results[[v, n]] = { source: "#{@site}/assets/#{rel}", missing: "#{code} #{info}" } }
          end
        end
      end
    end.each(&:join)
    found = results.count { |_, r| r[:file] }
    @log.call("live site: #{found}/#{jobs.size} frames (#{plan.variants.size} variants x #{plan.frames})")
    results
  end

  def probe_s3(plan, live)
    return { checked: 0, note: "no s3PublicUrl in the bundle" } unless plan.s3_base
    layouts = ["#{plan.folder}", "assets/#{plan.folder}"]
    answers = Hash.new(0)
    usable = []
    layouts.each do |layout|
      plan.variants.each do |v|
        code, body, = fetch_image(@s3, "#{plan.s3_base}/#{layout}/#{v}/0.webp")
        answers[code] += 1
        usable << layout if body
      end
    end
    used = 0
    usable.uniq.each do |layout|   # the bucket serves this layout: fill frames the site lacks
      live.each do |(v, n), r|
        next if r[:file]
        code, body, = fetch_image(@s3, "#{plan.s3_base}/#{layout}/#{v}/#{n}.webp")
        next unless body
        path = File.join(@downloads, "s3.mope.io", layout, v, "#{n}.webp")
        save(path, body)
        live[[v, n]] = { source: "#{plan.s3_base}/#{layout}/#{v}/#{n}.webp", file: path, sha256: Digest::SHA256.hexdigest(body) }
        used += 1
      end
    end
    summary = { base: plan.s3_base, checked: answers.values.sum, answers: answers.transform_keys(&:to_s), frames_used: used }
    @log.call("s3: #{summary[:checked]} probes #{summary[:answers]}; frames taken from s3: #{used}")
    summary
  end

  # -- 3. install ----------------------------------------------------------------------------------
  def install_live(plan, live)
    width = height = nil
    types = plan.variants.each_with_index.map do |variant, type|
      frames = (0...plan.frames).map do |n|
        r = live[[variant, n]]
        next { frame: n, missing: r[:missing] } unless r[:file]
        out = File.join(@target, type.to_s, "#{n}.png")
        convert_to(out) { |tmp| Pumpkins.run!(["convert", r[:file], "PNG32:#{tmp}"]) }
        w, h, = Pumpkins.identify(out)
        width ||= w
        height ||= h
        { frame: n, file: rel(out), source: r[:source], sha256: file_sha(out), width: w, height: h }
      end
      { type: type, name: variant, origin: "live mope.io (#{plan.folder}/#{variant})", frames: frames }
    end
    [types, [width || 284, height || 284].max]
  end

  def install_custom(plan, size, first_type)
    Pumpkins::CUSTOM.each_with_index.filter_map do |name, i|
      src = File.join(Pumpkins::PROJECT, "custom_assets", "pumpkins", "#{name}.png")
      unless File.exist?(src)
        @log.call("custom variant #{name}: #{src} missing; skipped")
        next
      end
      type = first_type + i
      base = File.join(@downloads, "custom", "#{name}_base.png")
      FileUtils.mkdir_p(File.dirname(base))
      w, h, = Pumpkins.identify(src)
      corners = [[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1]].flat_map { |x, y| ["-draw", "color #{x},#{y} floodfill"] }
      Pumpkins.run!(["convert", src, "-alpha", "set", "-fuzz", "8%", "-fill", "none", *corners,
                     "-trim", "+repage", "-resize", "#{size}x#{size}", "-background", "none", "-gravity", "center",
                     "-extent", "#{size}x#{size}", "PNG32:#{base}"])
      inside = Pumpkins.alpha_at(base, size / 2, size / 2)
      corner = Pumpkins.alpha_at(base, 0, 0)
      raise "#{name}: background removal failed (centre alpha #{inside}, corner alpha #{corner})" if inside < 0.99 || corner > 0.01
      frames = (0...plan.frames).map do |n|
        out = File.join(@target, type.to_s, "#{n}.png")
        angle = @o[:custom_frames] == "spin" ? (360.0 / plan.frames * n).round(1) : 0
        convert_to(out) do |tmp|
          Pumpkins.run!(["convert", base, "-background", "none", "-rotate", angle.to_s, "-gravity", "center",
                         "-extent", "#{size}x#{size}", "PNG32:#{tmp}"])
        end
        { frame: n, file: rel(out), source: "custom_assets/pumpkins/#{name}.png", rotation: angle,
          sha256: file_sha(out), width: size, height: size }
      end
      { type: type, name: name, origin: "custom (custom_assets/pumpkins/#{name}.png, background removed, #{@o[:custom_frames]} frames)",
        frames: frames }
    end
  end

  def convert_to(out)
    FileUtils.mkdir_p(File.dirname(out))
    tmp = "#{out}.tmp.png"
    yield tmp
    File.chmod(0o644, out) if File.exist?(out)
    File.rename(tmp, out)
  end

  def rel(path) = path.sub("#{@client_root}/", "")
  def file_sha(path) = Digest::SHA256.file(path).hexdigest

  # -- run ----------------------------------------------------------------------------------------------
  def run
    plan = Pumpkins.plan_from(bundles)
    @log.call("plan: #{plan.variants.size} live variants #{plan.variants.inspect}, #{plan.frames} frames each, " \
              "+ #{Pumpkins::CUSTOM.size} custom #{Pumpkins::CUSTOM.inspect}; target #{rel(@target)}")
    return plan if @o[:dry_run]
    live = fetch_live(plan)
    s3 = probe_s3(plan, live)
    live_types, size = install_live(plan, live)
    custom_types = install_custom(plan, size, plan.variants.size)
    manifest = {
      generated: Time.now.utc.iso8601, tool: "fetch_pumpkins.rb",
      client_path: "./assets/s/<season>/img/pumpkin/<pumpkinType>/<frame>.png (client L24937-24939)",
      season: @season, object_type: 131, frames: plan.frames, live_folder: plan.folder, s3: s3,
      note: "pumpkinType numbering is local: the original server's numbering is not in any archive",
      types: live_types + custom_types,
    }
    File.write(File.join(@target, "pumpkins.json"), JSON.pretty_generate(manifest))
    committed = File.join(Pumpkins::PROJECT, "pumpkins", "data", "manifest.json")
    FileUtils.mkdir_p(File.dirname(committed))
    File.write(committed, JSON.pretty_generate(manifest))
    complete = manifest[:types].count { |t| t[:frames].all? { |f| f[:file] } }
    @log.call("installed #{complete}/#{manifest[:types].size} pumpkin types into #{rel(@target)} " \
              "(types #{manifest[:types].map { |t| "#{t[:type]}=#{t[:name]}" }.join(', ')})")
    manifest
  end
end

if $PROGRAM_NAME == __FILE__
  options = { dry_run: false, refresh_site: false, custom_frames: "spin", workers: 8 }
  OptionParser.new do |o|
    o.banner = "usage: ruby fetch_pumpkins.rb [options]"
    o.on("--dry-run", "only read the bundle and print the plan") { options[:dry_run] = true }
    o.on("--refresh-site", "download the live bundles instead of using the newest live snapshot") { options[:refresh_site] = true }
    o.on("--custom-frames STYLE", %w[spin still], "custom variant frames: spin (72 degrees per frame) or still") { |v| options[:custom_frames] = v }
    o.on("--workers N", Integer, "parallel downloads (default 8, max 20)") { |v| options[:workers] = v.clamp(1, 20) }
  end.parse!
  PumpkinInstaller.new(options).run
end
