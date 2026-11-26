# frozen_string_literal: true

# Offline tests for fetch_ability_frames.rb (run: ruby tests/test_ability_frames.rb). The bundle
# fixture is written in the style of the live site's minified code.

require "minitest/autorun"
require_relative "../fetch_ability_frames"

ABILITY_BUNDLE = <<~'JS'
  var cfg={s3PublicUrl:`https://s3.mope.io/mope-beta`},cfg2={s3PublicUrl:`https://s3.mope.io/mope-prod`};
  var Jc=function(e){return e[e.spit=0]=`spit`,e[e.fire_tornado=1]=`fire_tornado`,e}({});
  var Ix=function(e){return e[e.DEFAULT=0]=`DEFAULT`,e[e.SUMMER21=1]=`SUMMER21`,e}({});
  var Lx=class extends bx{sprite;subspeciesEnum=Ix;Er=`spit`;};
  var GS=function(e){return e[e.DEFAULT=0]=`DEFAULT`,e[e.QUILIN=1]=`QUILIN`,e}({});
  class K{async init(){let t;if(!this.equippedItemId)t=`abilities/stomp`;else{let n=GS[e.Nb[this.equippedItemId]??GS.DEFAULT].toLowerCase();t=`${$.Yw.s3PublicUrl}/items/abilities/stomp/${n}`}}}
  var UC=function(e){return e[e.DEFAULT=0]=`DEFAULT`,e[e.GOLD=1]=`GOLD`,e}({});
  class W{async init(){let n=UC[e.Nb[this.equippedItemId]??UC.DEFAULT].toLowerCase();t=`${$.Yw.s3PublicUrl}/items/abilities/bite/${n}/ability_trexBite`}}
  x=td(`abilities/quill/quill`,`abilities/fireball/0/static/0`);
JS

class AbilityFramesTest < Minitest::Test
  def enums = LiveAssets::SiteParser.new({ "a.js" => ABILITY_BUNDLE }).enums

  def test_prod_bucket
    assert_equal "https://s3.mope.io/mope-prod", AbilityFrames.s3_base(ABILITY_BUNDLE)
  end

  def test_ability_object_folders_and_classes
    folders, classes = AbilityFrames.ability_objects(ABILITY_BUNDLE, enums)
    assert_equal %w[spit fire_tornado], folders
    assert_equal [[%w[DEFAULT SUMMER21], "spit"]], classes
  end

  def test_enum_templates_resolve_nested_index
    found = AbilityFrames.enum_templates(ABILITY_BUNDLE, enums)
    assert_equal [["stomp", %w[DEFAULT QUILIN], ""], ["bite", %w[DEFAULT GOLD], "/ability_trexBite"]], found
  end

  def test_literal_effects
    assert_equal %w[abilities/fireball/0/static/0 abilities/quill/quill abilities/stomp],
                 AbilityFrames.literal_effects(ABILITY_BUNDLE)
  end

  def test_next_frame
    assert_equal "https://x/a/spit1.webp", AbilityFrames.next_frame("https://x/a/spit0.webp")
    assert_equal "https://x/static/4.webp", AbilityFrames.next_frame("https://x/static/3.webp")
    assert_nil AbilityFrames.next_frame("https://x/a/ability.webp")
    assert_nil AbilityFrames.next_frame("https://x/a/#{AbilityFrames::MAX_FRAMES - 1}.webp")
  end

  def test_storage_path_keeps_host_and_stays_inside
    assert_equal "s3.mope.io/mope-prod/items/x/ability.webp",
                 AbilityFrames.storage_path("https://s3.mope.io/mope-prod/items/x/ability.webp")
    assert_equal "mope.io/assets/a.webp", AbilityFrames.storage_path("https://mope.io/assets/../a.webp")
    refute_includes AbilityFrames.storage_path("https://mope.io/../../etc/passwd"), ".."
  end
end
