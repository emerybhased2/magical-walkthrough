# frozen_string_literal: true

# Offline tests for fetch_pumpkins.rb (run: ruby tests/test_pumpkins.rb).

require "minitest/autorun"
require_relative "../fetch_pumpkins"

class PumpkinPlanTest < Minitest::Test
  BUNDLE = 'var x={s3PublicUrl:`https://s3.mope.io/mope-beta`},y={s3PublicUrl:`https://s3.mope.io/mope-prod`};' \
           'class P{async init(){await super.init();let e=`terrain/pumpkin/${[`normal`,`jack`,`gold`][this.subspecies]}`,' \
           't=[];for(let n=0;n<5;n++)t.push(`${e}/${n}`);let n=await td(...t);return this}}'

  def test_plan_reads_variants_frames_and_bucket
    plan = Pumpkins.plan_from(BUNDLE)
    assert_equal "terrain/pumpkin", plan.folder
    assert_equal %w[normal jack gold], plan.variants
    assert_equal 5, plan.frames
    assert_equal "https://s3.mope.io/mope-prod", plan.s3_base
  end

  def test_plan_fails_loudly_without_the_class
    assert_raises(RuntimeError) { Pumpkins.plan_from("var a=1;") }
  end

  def test_custom_variants_follow_the_live_ones
    assert_equal %w[amber sapphire amethyst black_hole], Pumpkins::CUSTOM
    Pumpkins::CUSTOM.each { |name| assert File.exist?(File.join(Pumpkins::PROJECT, "custom_assets", "pumpkins", "#{name}.png")), name }
  end
end
