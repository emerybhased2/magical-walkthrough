# frozen_string_literal: true

require "set"
require_relative "js_scan"

# Reads the live site's bundles as text and derives the asset paths the game loads.
# The rules mirror the bundle's own path builders (see live/README.md):
#   texture loader   relative name -> ./assets/<name>.webp
#   animal folder    animals/<volcano if lava else biome>/<animal>/<subspecies folder>
#                    (the subspecies folder is the enum name lower-cased plus "/", none for DEFAULT)
#   animal parts     <animal folder> + literal part name (this.<base>+`part`)
#   animal UI icon   <animal folder><animal>.ui.webp
module LiveAssets
  IMAGE_EXT = /\.(?:webp|png|jpe?g|gif|svg|ico|avif)\z/i
  ASSET_LITERAL = %r{(?:\./|/)?assets/[A-Za-z0-9_\-./]+?\.(?:webp|png|jpe?g|gif|svg|ico|avif|json)(?=[`"'?#)\s])}i
  BIOMES = %w[land ocean arctic desert volcano].freeze

  Animal = Struct.new(:name, :biome_folder, :subspecies, :lava, keyword_init: true)

  class SiteParser
    attr_reader :sources

    # sources: { "c/xxxx.js" => text, ... } (bundles and stylesheets of one site build)
    def initialize(sources)
      @sources = sources
      @js = sources.select { |k, _| k.end_with?(".js") }.values.join("\n")
      @css = sources.select { |k, _| k.end_with?(".css") }.values.join("\n")
    end

    # TypeScript enums compiled as  X=function(e){return e[e.NAME=0]=`NAME`,...,e}({})
    def enums
      @enums ||= begin
        found = {}
        @js.scan(/([A-Za-z_$][\w$]*)=function\((\w)\)\{return ?((?:\2\[\2\.\w+=\d+\]=`\w+`,)+)\2\}\(\{\}\)/) do |name, _, body|
          found[name] = body.scan(/\[\w\.(\w+)=(\d+)\]/).map { |n, v| [v.to_i, n] }.sort.map(&:last)
        end
        found
      end
    end

    # The animal table: <var>={name:um(`name`,{...config...}),...}; the largest such object wins.
    def animals
      @animals ||= begin
        best = []
        @js.to_enum(:scan, /[A-Za-z_$][\w$]*=\{[a-z0-9_]+:([A-Za-z_$][\w$]*)\(`[a-z0-9_]+`,\{/).each do
          match = Regexp.last_match
          factory = match[1]
          open = match.begin(0) + match[0].index("{")
          close = JsScan.find_close(@js, open)
          next unless close
          body = @js[open..close]
          list = parse_animal_entries(body, factory)
          best = list if list.length > best.length
        end
        best
      end
    end

    def parse_animal_entries(body, factory)
      list = []
      body.to_enum(:scan, /(?<=[{,])([a-z0-9_]+):#{Regexp.escape(factory)}\(`([a-z0-9_]+)`,(?=\{)/).each do
        m = Regexp.last_match
        config_open = m.end(0)
        config_close = JsScan.find_close(body, config_open)
        next unless config_close
        props = JsScan.top_level_props(body[config_open..config_close])
        biome = props["biome"].to_s[/\A`(\w*)`\z/, 1] || props["biome"].to_s[/\A[\w$]+\.(\w+)\z/, 1]
        next if biome.nil? || biome.empty?
        lava = props["animalResource"].to_s.match?(/type:[\w$]+\.lava\b/)
        enum_name = props["subspeciesEnum"].to_s[/\A([A-Za-z_$][\w$]*)\z/, 1]
        subspecies = enum_name && enums[enum_name] ? enums[enum_name] : ["DEFAULT"]
        list << Animal.new(name: m[2], biome_folder: lava ? "volcano" : biome, subspecies: subspecies, lava: lava)
      end
      list
    end

    # Part names appended to an animal's folder: this.<getter>+`part` where the getter is the
    # one whose body builds `animals/...` (detected, not assumed by name).
    def base_getters
      @base_getters ||= @js.scan(/get ([A-Za-z_$][\w$]*)\(\)\{(?:(?!get [\w$]+\(\)\{).){0,400}?return`animals\//m).flatten.uniq
    end

    def part_names
      @part_names ||= begin
        parts = Set.new
        base_getters.each do |getter|
          g = Regexp.escape(getter)
          @js.scan(/this\.#{g}\+`([A-Za-z0-9_\-]+)`/) { |(p)| parts << p }
          @js.scan(/this\.#{g}\+"([A-Za-z0-9_\-]+)"/) { |(p)| parts << p }
          @js.scan(/\$\{this\.#{g}\}([A-Za-z0-9_\-]+)(?:\.webp)?`/) { |(p)| parts << p }
        end
        parts << "body" # ./assets/animals/land/fox/body.webp is referenced literally
        parts.to_a.sort
      end
    end

    # Parts that belong to one animal (prefixed with its name) vs. shared ones (ability, tail, ...).
    def parts_for(animal)
      specific = part_names.select { |p| owned_by?(p, animal.name) }
      generic = part_names.reject { |p| animals.any? { |a| owned_by?(p, a.name) } }
      (generic + specific + ["#{animal.name}.ui", "ability2"]).uniq
    end

    # A part belongs to an animal when it starts with the animal's name, with or without the
    # underscores of the table key (kangaroo_rat -> kangaroorat1, snowy_owl -> snowyowl_head).
    def owned_by?(part, name)
      [name, name.delete("_"), name.tr("_", "-")].uniq.any? do |variant|
        part == variant || part.match?(/\A#{Regexp.escape(variant)}(?:[_\-]|\d)/)
      end
    end

    def subspecies_folder(name)
      name == "DEFAULT" ? "" : "#{name.downcase}/"
    end

    # Literal texture-loader names (no ${...}): ed(`terrain/flow`) -> assets/terrain/flow.webp
    def texture_literals
      loaders = @js.scan(/(?:async )?function ([A-Za-z_$][\w$]*)\(\w+,\w+,\w+=\[\]\)\{\w+=([A-Za-z_$][\w$]*)\(\w+,\w+\)/).map(&:first)
      names = Set.new
      loaders.each do |fn|
        @js.scan(/(?<![\w$.])#{Regexp.escape(fn)}\(`([^`$]+)`/) { |(n)| names << n }
      end
      generated = generated_textures
      names.reject { |n| n.end_with?("/") || generated.any? { |g| n.start_with?(g) } }
           .map { |n| normalise_texture(n) }.uniq.sort
    end

    # Textures the bundle draws itself at start-up (this.save(`circle_${t}.webp`)): not files.
    def generated_textures
      @generated_textures ||= @js.scan(/\.save\(`([A-Za-z0-9_\-]+)/).flatten.uniq
    end

    # The bundle's own normaliser: prefix ./assets/ unless absolute, append .webp unless present.
    def normalise_texture(name, ext = "webp")
      path = name
      path = "./assets/#{path}" unless path.start_with?("http://", "https://", "./assets")
      path += ".#{ext}" unless path.end_with?(".#{ext}")
      clean(path)
    end

    def clean(path)
      path.sub(%r{\A\./}, "").sub(%r{\A/}, "").gsub(%r{/{2,}}, "/")
    end

    # Every literal asset path in JavaScript and CSS (no runtime parts).
    def literal_assets
      paths = Set.new
      (@js + "\n" + @css).scan(ASSET_LITERAL) { paths << clean(Regexp.last_match(0)) }
      @css.scan(/url\(\s*["']?([^"')]+)["']?\s*\)/) do |(u)|
        next if u.start_with?("data:", "http")
        paths << clean(u.sub(%r{\A(\.\./)+}, ""))
      end
      paths.select { |p| p.match?(IMAGE_EXT) || p.end_with?(".json") }.sort
    end

    # Food names: arrays of plain names keyed by biome ({land:[`a`,`b`],ocean:[...]}) in the bundle.
    def food_names
      names = Set.new
      @js.scan(%r~(?<=[{,])(?:#{BIOMES.join('|')}):\[((?:`[a-z0-9_/]+`,?)+)\]~) do |(list)|
        list.scan(%r~`([a-z0-9_/]+)`~) { |(n)| names << n }
      end
      names.to_a.sort
    end

    def rarity_names
      found = Set.new
      @js.scan(/\{((?:(?:legendary|epic|rare|uncommon|common|mythic):\d+,?){3,})\}/) do |(m)|
        m.scan(/([a-z]+):\d+/) { |(n)| found << n }
      end
      found.to_a.sort
    end

    # All candidate paths with the rule that produced them (each is verified by fetching).
    def candidates
      out = {}
      add = ->(path, rule) { out[clean(path)] ||= rule }
      literal_assets.each { |p| add.(p, "literal") }
      texture_literals.each { |p| add.(p, "texture-literal") }
      food_names.each { |f| add.("assets/foods/#{f}.webp", "food-list") }
      BIOMES.each { |b| add.("assets/UI/bestiary/scene-#{b}.webp", "bestiary-biome") }
      rarity_names.each do |tier|
        add.("assets/UI/achievements/medal-#{tier}.webp", "medal-tier")
        add.("assets/UI/achievements/medal-#{tier}-locked.webp", "medal-tier")
      end
      out
    end

    # Animal folders: [animal, subspecies, folder, [parts]] -- the default folder is probed with every
    # candidate part, subspecies folders with the parts the default folder turned out to have.
    def animal_folders
      animals.flat_map do |a|
        a.subspecies.map do |sub|
          [a, sub, "assets/animals/#{a.biome_folder}/#{a.name}/#{subspecies_folder(sub)}"]
        end
      end
    end

    # Further code chunks the bundles import (Vite: "./Xxxx.js", "../c/Xxxx.js").
    def self.chunk_references(text)
      text.scan(%r{["'`]((?:\.\.?/)(?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_\-]+\.(?:js|css))["'`]}).flatten.uniq
    end
  end
end
