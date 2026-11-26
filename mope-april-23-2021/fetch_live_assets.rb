#!/usr/bin/env ruby
# frozen_string_literal: true

# fetch_live_assets.rb -- snapshot the images of the LIVE mope.io site (the 2026 rebuild).
#
# The live site is a different game build from the archived 2021/2025 client: its images are
# WebP files under assets/animals/<biome>/<animal>/<subspecies>/, assets/foods/, assets/UI/, ...
# and it answers every unknown path with its HTML app shell (HTTP 200). There is no listing, so
# the paths are derived from the site's own bundles, read as text (never executed):
#
#   1. site      download index.html and every JS/CSS bundle it loads (and their imports)
#   2. plan      derive candidate paths: literal asset paths, texture-loader names, the food list,
#                and for each of the bundle's animals (table + subspecies enums) its folder with
#                the part names the bundle appends to it (live/README.md explains each rule)
#   3. fetch     GET each candidate politely (default 3 requests/s, 2 workers, back-off, stops if
#                the site keeps refusing); only real image bytes count -- the HTML fallback is a miss
#   4. animals   default folders are probed with every candidate part; subspecies folders with
#                the parts their default folder turned out to have
#   5. report    manifests + summary; found files are stored read-only
#
# Usage:
#   ruby fetch_live_assets.rb                    # everything
#   ruby fetch_live_assets.rb --plan-only        # snapshot the bundles and write the plan only
#   ruby fetch_live_assets.rb --rate 2 --budget 3000
#   ruby fetch_live_assets.rb --resume           # continue an interrupted snapshot (default when state exists)
#
# Output (git-ignored): archive/live/<snapshot-date>/{site,files,manifests}/
# Shop items on s3.mope.io are not fetched: the bucket cannot be listed and item ids are not in the bundle.

$stdout.sync = true
require "optparse"
require "json"
require "digest"
require "fileutils"
require "time"
require_relative "live_assets/js_scan"
require_relative "live_assets/site_parser"
require_relative "live_assets/formats"
require_relative "live_assets/fetcher"

module LiveAssets
  VERSION = "1.0.0"
  PROJECT = __dir__

  def self.category(path)
    case path
    when %r{\Aassets/animals/} then "animals"
    when %r{\Aassets/foods/} then "foods"
    when %r{\Aassets/UI/} then "ui"
    when %r{\Aassets/abilities/} then "abilities"
    when %r{\Aassets/terrain/} then "terrain"
    else "other"
    end
  end

  # A storage path that stays inside the output folder.
  def self.safe_relative(path)
    parts = path.split("/").reject { |s| s.empty? || s == "." || s == ".." }.map { |s| s.gsub(/[\x00-\x1f<>:"\\|?*]/, "_") }
    parts.empty? ? "_" : parts.join("/")
  end

  class Snapshot
    def initialize(options)
      @o = options
      @site = options[:site].chomp("/")
      @root = File.expand_path(File.join(options[:output], options[:snapshot]), PROJECT)
      @manifests = File.join(@root, "manifests")
      FileUtils.mkdir_p(@manifests)
      @log_lock = Mutex.new
      @fetcher = Fetcher.new(rate_per_second: options[:rate], log: method(:log))
      @results = {}
      @state_path = File.join(@manifests, "results.jsonl")
      @state_lock = Mutex.new
      load_state unless options[:refresh]
    end

    def log(message)
      @log_lock.synchronize { puts "[#{Time.now.utc.strftime('%H:%M:%S')}] #{message}" }
    end

    def load_state
      return unless File.exist?(@state_path)
      File.foreach(@state_path) do |line|
        r = JSON.parse(line, symbolize_names: true)
        next if r[:format] == "unknown" || r[:problems].to_a.any? { |m| m.start_with?("HTTP 5") }   # re-check these
        @results[r[:path]] = r
      end
      log("resume: #{@results.size} paths already checked")
    end

    # -- 1. site -------------------------------------------------------------------------------
    def snapshot_site
      site_dir = File.join(@root, "site")
      status, type, body, = @fetcher.get("#{@site}/")
      raise "homepage returned HTTP #{status}" unless status == 200 && type.include?("html")
      write(File.join(site_dir, "index.html"), body, readonly: false)
      files = { "index.html" => { bytes: body.bytesize, sha256: Digest::SHA256.hexdigest(body) } }
      queue = body.scan(/(?:src|href)="(\.?\/?[^"]+\.(?:js|css))"/).flatten.map { |r| r.sub(%r{\A\./}, "").sub(%r{\A/}, "") }.uniq
      sources = {}
      seen = {}
      until queue.empty? || seen.size >= 400
        rel = queue.shift
        next if seen[rel] || rel.include?("..") || rel.start_with?("http")
        seen[rel] = true
        status, _type, text, = @fetcher.get("#{@site}/#{rel}")
        next unless status == 200
        text = text.dup.force_encoding("UTF-8").scrub
        next if text.lstrip.start_with?("<!doctype html", "<html", "<!DOCTYPE html")
        sources[rel] = text
        write(File.join(site_dir, LiveAssets.safe_relative(rel)), text.b, readonly: false)
        files[rel] = { bytes: text.bytesize, sha256: Digest::SHA256.hexdigest(text) }
        base = File.dirname(rel)
        SiteParser.chunk_references(text).each do |ref|
          resolved = File.expand_path(ref, "/#{base}").sub(%r{\A/}, "")
          queue << resolved unless seen[resolved]
        end
      end
      log("site: #{sources.size} bundles/stylesheets (#{(sources.values.sum(&:bytesize) / 1024.0 / 1024).round(1)} MB)")
      [sources, files]
    end

    # -- 3. fetch --------------------------------------------------------------------------------
    def check(paths, rule_for)
      todo = paths.reject { |p| @results.key?(p) }
      budget_left = @o[:budget] - @fetcher.requests
      if todo.size > budget_left
        log("budget: #{todo.size} paths to check, #{budget_left} requests left; the rest is skipped (raise --budget)")
        todo = todo.first([budget_left, 0].max)
      end
      queue = Queue.new
      todo.each { |p| queue << p }
      done = 0
      workers = Array.new([@o[:workers], todo.size].min) do
        Thread.new do
          loop do
            path = begin
              queue.pop(true)
            rescue ThreadError
              break
            end
            record(path, rule_for.call(path))
            @state_lock.synchronize do
              done += 1
              log("checked #{done}/#{todo.size}") if (done % 250).zero? || done == todo.size
            end
          end
          @fetcher.close_sessions
        end
      end
      workers.each(&:join)
      paths.map { |p| @results[p] }.compact
    end

    def record(path, rule)
      status, type, body, = @fetcher.get("#{@site}/#{path}")
      info = status == 200 ? Formats.inspect_bytes(body) : { format: nil, ok: false, problems: ["HTTP #{status}"] }
      found = status == 200 && info[:ok] && info[:format] != "html"
      result = { path: path, rule: rule, status: status, content_type: type, found: found, format: info[:format],
                 width: info[:width], height: info[:height], problems: info[:problems] }
      if found
        result[:bytes] = body.bytesize
        result[:sha256] = Digest::SHA256.hexdigest(body)
        write(File.join(@root, "files", LiveAssets.safe_relative(path)), body)
      end
      @state_lock.synchronize do
        @results[path] = result
        File.open(@state_path, "a") { |f| f.puts(JSON.generate(result)) }
      end
    rescue Throttled
      raise
    rescue StandardError => e
      @state_lock.synchronize { log("  error for #{path}: #{e.class}: #{e.message}") }
    end

    def write(path, bytes, readonly: true)
      FileUtils.mkdir_p(File.dirname(path))
      File.chmod(0o644, path) if File.exist?(path)
      File.binwrite(path, bytes)
      File.chmod(0o444, path) if readonly
    end

    # -- run -------------------------------------------------------------------------------------
    def run
      started = Time.now
      sources, site_files = snapshot_site
      parser = SiteParser.new(sources)
      animals = parser.animals
      plan = parser.candidates
      folders = parser.animal_folders
      log("plan: #{plan.size} direct paths, #{animals.size} animals in #{folders.size} folders, " \
          "#{parser.part_names.size} part names, #{parser.food_names.size} foods")
      File.write(File.join(@manifests, "site.json"), JSON.pretty_generate(site: @site, fetched: Time.now.utc.iso8601, files: site_files))
      File.write(File.join(@manifests, "animals.json"), JSON.pretty_generate(animals.map(&:to_h)))
      File.write(File.join(@manifests, "plan.json"), JSON.pretty_generate(
        direct: plan, part_names: parser.part_names, foods: parser.food_names,
        folders: folders.map { |a, sub, dir| { animal: a.name, subspecies: sub, folder: dir } }
      ))
      @plan_paths = plan.keys
      return summary(started, animals, parser, plan_only: true) if @o[:plan_only]

      log("fetch: direct paths")
      check(plan.keys, ->(p) { plan[p] })

      log("fetch: animal default folders")
      default_paths = {}
      animals.each do |a|
        dir = "assets/animals/#{a.biome_folder}/#{a.name}/"
        parser.parts_for(a).each { |part| default_paths["#{dir}#{part}.webp"] = "animal-part" }
      end
      check(default_paths.keys, ->(p) { default_paths[p] })

      log("fetch: subspecies folders")
      sub_paths = {}
      folders.each do |a, sub, dir|
        next if sub == "DEFAULT"
        base = "assets/animals/#{a.biome_folder}/#{a.name}/"
        found_parts = parser.parts_for(a).select { |part| @results.dig("#{base}#{part}.webp", :found) }
        (found_parts | ["#{a.name}.ui", "ability", "ability2"]).each { |part| sub_paths["#{dir}#{part}.webp"] = "subspecies-part" }
      end
      check(sub_paths.keys, ->(p) { sub_paths[p] })
      summary(started, animals, parser)
    rescue Throttled => e
      log("stopped: #{e.message}. Found files are kept; run again later to resume.")
      summary(started, animals || [], parser)
      exit 4
    end

    def summary(started, animals, parser, plan_only: false)
      found = @results.values.select { |r| r[:found] }
      by_category = found.group_by { |r| LiveAssets.category(r[:path]) }.transform_values(&:size).sort.to_h
      per_animal = animals.map do |a|
        files = found.select { |r| r[:path].start_with?("assets/animals/#{a.biome_folder}/#{a.name}/") }
        { animal: a.name, biome: a.biome_folder, subspecies: a.subspecies, files: files.size }
      end
      report = {
        generated: Time.now.utc.iso8601, tool: "fetch_live_assets.rb #{VERSION}", site: @site, snapshot: @o[:snapshot],
        seconds: (Time.now - started).round, requests_this_run: @fetcher.requests, plan_only: plan_only,
        paths_checked: @results.size, files_found: found.size,
        bytes: found.sum { |r| r[:bytes].to_i }, by_category: by_category,
        by_format: found.group_by { |r| r[:format] }.transform_values(&:size),
        decode_problems: found.count { |r| !r[:problems].to_a.empty? },
        animals_in_bundle: animals.size, animals_with_files: per_animal.count { |a| a[:files].positive? },
        part_names: parser&.part_names&.size, per_animal: per_animal,
        missing_direct: @results.values.reject { |r| r[:found] || r[:rule].to_s.include?("part") }
                                .select { |r| @plan_paths.nil? || @plan_paths.include?(r[:path]) }.map { |r| r[:path] }.sort
      }
      File.write(File.join(@manifests, "report.json"), JSON.pretty_generate(report))
      File.write(File.join(@manifests, "files.json"), JSON.pretty_generate(found.sort_by { |r| r[:path] }))
      puts
      puts "paths checked: #{report[:paths_checked]} (#{report[:requests_this_run]} requests this run)"
      puts "files found:   #{report[:files_found]} (#{(report[:bytes] / 1024.0 / 1024).round(1)} MB), by category: #{by_category}"
      puts "animals:       #{report[:animals_with_files]}/#{report[:animals_in_bundle]} with files"
      puts "output:        #{@root}"
      report
    end
  end

  def self.parse_options(argv)
    options = { site: "https://mope.io", output: "archive/live", snapshot: Time.now.utc.strftime("%Y-%m-%d"),
                rate: 3.0, workers: 2, budget: 12_000, refresh: false, plan_only: false }
    OptionParser.new do |o|
      o.banner = "usage: ruby fetch_live_assets.rb [options]"
      o.on("--site URL", "site to snapshot (default https://mope.io)") { |v| options[:site] = v }
      o.on("--output DIR", "output folder (default archive/live)") { |v| options[:output] = v }
      o.on("--snapshot NAME", "snapshot folder name (default: today's UTC date)") { |v| options[:snapshot] = v }
      o.on("--rate N", Float, "requests per second over all workers (default 3; 0 = no cap, workers only)") do |v|
        options[:rate] = v <= 0 ? 0.0 : v.clamp(0.1, 100.0)
      end
      o.on("--workers N", Integer, "parallel workers / concurrent connections (default 2, max 64)") { |v| options[:workers] = v.clamp(1, 64) }
      o.on("--budget N", Integer, "maximum requests this run (default 12000)") { |v| options[:budget] = v }
      o.on("--refresh", "ignore earlier results of this snapshot") { options[:refresh] = true }
      o.on("--plan-only", "snapshot the bundles and write the plan; fetch no images") { options[:plan_only] = true }
    end.parse!(argv)
    raise OptionParser::InvalidArgument, "--site must be https" unless options[:site].start_with?("https://")
    options
  end
end

if $PROGRAM_NAME == __FILE__
  options = LiveAssets.parse_options(ARGV)
  LiveAssets::Snapshot.new(options).run
end
