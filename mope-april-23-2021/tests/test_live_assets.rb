# frozen_string_literal: true

# Offline tests for fetch_live_assets.rb (run: ruby tests/test_live_assets.rb). Fixtures are
# small synthetic bundles written in the style of the live site's minified code.

require "minitest/autorun"
require_relative "../live_assets/js_scan"
require_relative "../live_assets/site_parser"
require_relative "../live_assets/formats"
require_relative "../fetch_live_assets"

BUNDLE = <<~'JS'
  var Rp=function(e){return e[e.DEFAULT=0]=`DEFAULT`,e}({}),zp=function(e){return e[e.DEFAULT=0]=`DEFAULT`,e[e.AQUA=1]=`AQUA`,e[e.FORGOTTEN_ONE=2]=`FORGOTTEN_ONE`,e}({});
  function um(e,t){return{biome:t.biome,subspeciesEnum:t.subspeciesEnum||Rp}}
  var lm={fox:um(`fox`,{comfortZones:new Pp([0]),biome:`land`,label:`a,b}`,subspeciesOverrides:new Map([[1,{biome:`ocean`}]])}),kangaroo_rat:um(`kangaroo_rat`,{biome:rl.desert,subspeciesEnum:zp}),lava_dragon:um(`lava_dragon`,{biome:`land`,animalResource:{type:Lp.lava}})};
  class A{get Ag(){if(this.#e)return`${$.Yw.s3PublicUrl}/items/${this.#e}/`;let e=Y[this.species];return`animals/${this.animalConfig.biome}/${e}/${zg(this.animalConfig,this.subspecies)}`}
  async load(){this.a=await ed(this.Ag+`tail`);this.b=await ed(this.Ag+`kangaroorat1`);this.c=await ed(`${this.Ag}head`)}}
  async function ed(e,t,n=[]){e=nd(e,t);return await xl(e,n)}
  x=ed(`terrain/flow`),y=ed(`circle_${q}`),z=ed(`/animals/arctic/sabertooth_tiger/ability`),w=ed(`square`),v=ed(`terrain/arctic/`+q);
  for(let t of[32,64]){this.draw(e=>{});let n=this.save(`circle_${t}.webp`)};let s=this.save(`square.webp`);
  var food={land:[`watermelon/edible`,`carrot/carrot`],ocean:[`kelp/edible`]};
  var r={legendary:4,epic:3,rare:2};
  var logo=`./assets/logo.webp`,bad=`./assets/${q}.webp`;
  import("./BgSVSR2j.js");import("../packages/Cs-Pi3NN.js");
JS

CSS = ".a{background:url(../assets/UI/banners/default.webp)} .b{background:url(data:image/png;base64,AAAA)}"

class JsScanTest < Minitest::Test
  def test_find_close_skips_strings_and_templates
    src = 'x={a:`}${{b:1}}`,c:"}",d:[1,{e:2}]}'
    assert_equal src.length - 1, LiveAssets::JsScan.find_close(src, 2)
  end

  def test_top_level_props
    props = LiveAssets::JsScan.top_level_props('{biome:`land`,x:{biome:`ocean`},label:`a,b}`,f:g(1,2)}')
    assert_equal "`land`", props["biome"]
    assert_equal "{biome:`ocean`}", props["x"]
    assert_equal "g(1,2)", props["f"]
  end
end

class SiteParserTest < Minitest::Test
  def setup
    @parser = LiveAssets::SiteParser.new("c/main.js" => BUNDLE, "c/site.css" => CSS)
  end

  def test_enums
    assert_equal %w[DEFAULT AQUA FORGOTTEN_ONE], @parser.enums["zp"]
    assert_equal %w[DEFAULT], @parser.enums["Rp"]
  end

  def test_animals_use_top_level_biome_subspecies_and_lava
    animals = @parser.animals.to_h { |a| [a.name, a] }
    assert_equal %w[fox kangaroo_rat lava_dragon], animals.keys
    assert_equal "land", animals["fox"].biome_folder           # not the override's ocean
    assert_equal "desert", animals["kangaroo_rat"].biome_folder
    assert_equal %w[DEFAULT AQUA FORGOTTEN_ONE], animals["kangaroo_rat"].subspecies
    assert_equal "volcano", animals["lava_dragon"].biome_folder
    assert_equal ["DEFAULT"], animals["fox"].subspecies
  end

  def test_parts_and_ownership
    assert_equal ["Ag"], @parser.base_getters
    assert_equal %w[body head kangaroorat1 tail], @parser.part_names
    rat = @parser.animals.find { |a| a.name == "kangaroo_rat" }
    fox = @parser.animals.find { |a| a.name == "fox" }
    assert_includes @parser.parts_for(rat), "kangaroorat1"
    refute_includes @parser.parts_for(fox), "kangaroorat1"
    assert_includes @parser.parts_for(fox), "fox.ui"
    folders = @parser.animal_folders.map(&:last)
    assert_includes folders, "assets/animals/desert/kangaroo_rat/forgotten_one/"
    assert_includes folders, "assets/animals/land/fox/"
  end

  def test_direct_candidates
    c = @parser.candidates
    assert_equal "literal", c["assets/logo.webp"]
    assert_equal "literal", c["assets/UI/banners/default.webp"]
    assert_equal "texture-literal", c["assets/terrain/flow.webp"]
    assert_equal "texture-literal", c["assets/animals/arctic/sabertooth_tiger/ability.webp"]
    assert_equal "food-list", c["assets/foods/watermelon/edible.webp"]
    assert_equal "medal-tier", c["assets/UI/achievements/medal-epic-locked.webp"]
    refute c.keys.any? { |k| k.include?("${") || k.include?("circle_") || k.include?("square") || k.end_with?("/.webp") }
  end

  def test_chunk_references
    assert_equal ["./BgSVSR2j.js", "../packages/Cs-Pi3NN.js"], LiveAssets::SiteParser.chunk_references(BUNDLE)
  end
end

class FormatsTest < Minitest::Test
  def test_webp_vp8l
    bits = (299) | (199 << 14)
    body = "\x2F".b + [bits].pack("V")
    data = "RIFF".b + [4 + 8 + body.bytesize].pack("V") + "WEBPVP8L".b + [body.bytesize].pack("V") + body
    info = LiveAssets::Formats.inspect_bytes(data)
    assert_equal ["webp", 300, 200, true], info.values_at(:format, :width, :height, :ok)
  end

  def test_svg_with_doctype
    svg = %(<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x">\n<svg xmlns="http://www.w3.org/2000/svg"></svg>)
    assert_equal ["svg", true], LiveAssets::Formats.inspect_bytes(svg).values_at(:format, :ok)
  end

  def test_html_fallback_is_not_an_image
    info = LiveAssets::Formats.inspect_bytes("<!doctype html><html><head><title>mope.io</title>")
    assert_equal "html", info[:format]
    refute info[:ok]
  end

  def test_png_header
    data = "\x89PNG\r\n\x1a\n".b + [13].pack("N") + "IHDR" + [7, 5].pack("NN") + "\x08\x06\x00\x00\x00".b + "CRC!" +
           [0].pack("N") + "IEND" + "CRC!"
    assert_equal ["png", 7, 5, true], LiveAssets::Formats.inspect_bytes(data).values_at(:format, :width, :height, :ok)
  end
end

class HelpersTest < Minitest::Test
  def test_safe_relative_and_category
    assert_equal "assets/etc/passwd", LiveAssets.safe_relative("assets/../../etc/passwd")
    assert_equal "animals", LiveAssets.category("assets/animals/land/fox/body.webp")
    assert_equal "foods", LiveAssets.category("assets/foods/carrot/carrot.webp")
    assert_equal "ui", LiveAssets.category("assets/UI/passes/plain.webp")
  end

  def test_options_reject_plain_http
    assert_raises(OptionParser::InvalidArgument) { LiveAssets.parse_options(["--site", "http://mope.io"]) }
    assert_equal 50.0, LiveAssets.parse_options(["--rate", "50"])[:rate]
    assert_equal 0.0, LiveAssets.parse_options(["--rate", "0"])[:rate]
    assert_equal 20, LiveAssets.parse_options(["--workers", "20"])[:workers]
    assert_equal 60, LiveAssets.parse_options(["--workers", "60"])[:workers]
    assert_equal 64, LiveAssets.parse_options(["--workers", "99"])[:workers]
  end
end
