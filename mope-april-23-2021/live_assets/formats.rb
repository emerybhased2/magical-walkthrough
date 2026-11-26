# frozen_string_literal: true

# Identifies a downloaded file from its bytes and reads its dimensions. The live site answers
# missing paths with its HTML app shell (HTTP 200), so only real image bytes count as found.
module LiveAssets
  module Formats
    module_function

    def u16le(b, o) = b.getbyte(o) | (b.getbyte(o + 1) << 8)
    def u24le(b, o) = b.getbyte(o) | (b.getbyte(o + 1) << 8) | (b.getbyte(o + 2) << 16)
    def u32le(b, o) = b[o, 4].unpack1("V")
    def u32be(b, o) = b[o, 4].unpack1("N")
    def u16be(b, o) = b[o, 2].unpack1("n")

    # => { format:, width:, height:, ok:, problems: [] }
    def inspect_bytes(bytes)
      b = bytes.b
      return webp(b) if b.bytesize >= 25 && b[0, 4] == "RIFF" && b[8, 4] == "WEBP"
      return png(b) if b.bytesize >= 33 && b[0, 8] == "\x89PNG\r\n\x1a\n".b
      return jpeg(b) if b.bytesize >= 4 && b.getbyte(0) == 0xFF && b.getbyte(1) == 0xD8
      return gif(b) if b.bytesize >= 10 && b[0, 3] == "GIF"
      head = b[0, 2048].force_encoding("UTF-8").scrub.lstrip
      if head.match?(/\A(<\?xml[^>]*>\s*)?(<!--.*?-->\s*)*(<!DOCTYPE svg[^>]*>\s*)?(<!--.*?-->\s*)*<svg\b/mi)
        return { format: "svg", width: nil, height: nil, ok: b.include?("</svg>"), problems: b.include?("</svg>") ? [] : ["no </svg>"] }
      end
      if head.match?(/\A(<!doctype html|<html)/i)
        return { format: "html", width: nil, height: nil, ok: false, problems: ["HTML page (the site's fallback for a missing file)"] }
      end
      if head.start_with?("{", "[")
        return { format: "json", width: nil, height: nil, ok: true, problems: [] }
      end
      { format: "unknown", width: nil, height: nil, ok: false, problems: ["unrecognised bytes"] }
    end

    def webp(b)
      problems = []
      riff = u32le(b, 4)
      problems << "RIFF size #{riff + 8} exceeds the file (#{b.bytesize} bytes): truncated" if riff + 8 > b.bytesize
      chunk = b[12, 4]
      w = h = nil
      needed = { "VP8 " => 30, "VP8L" => 25, "VP8X" => 30 }[chunk]
      if needed && b.bytesize < needed
        return { format: "webp", width: nil, height: nil, ok: false, problems: problems << "#{chunk.strip} header truncated" }
      end
      case chunk
      when "VP8 "
        problems << "bad VP8 start code" unless b.getbyte(23) == 0x9D && b.getbyte(24) == 0x01 && b.getbyte(25) == 0x2A
        w = u16le(b, 26) & 0x3FFF
        h = u16le(b, 28) & 0x3FFF
      when "VP8L"
        problems << "bad VP8L signature" unless b.getbyte(20) == 0x2F
        bits = u32le(b, 21)
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
      when "VP8X"
        w = u24le(b, 24) + 1
        h = u24le(b, 27) + 1
      else
        problems << "unknown WebP chunk #{chunk.inspect}"
      end
      { format: "webp", width: w, height: h, ok: problems.empty?, problems: problems }
    end

    def png(b)
      problems = []
      problems << "no IEND chunk (truncated?)" unless b[-12, 12]&.include?("IEND")
      { format: "png", width: u32be(b, 16), height: u32be(b, 20), ok: problems.empty?, problems: problems }
    end

    def jpeg(b)
      i = 2
      w = h = nil
      while i + 9 < b.bytesize && b.getbyte(i) == 0xFF
        marker = b.getbyte(i + 1)
        len = u16be(b, i + 2)
        if [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF].include?(marker)
          h = u16be(b, i + 5)
          w = u16be(b, i + 7)
          break
        end
        i += 2 + len
      end
      problems = []
      problems << "no SOF header" unless w
      problems << "no EOI marker (truncated?)" unless b.rstrip.end_with?("\xFF\xD9".b)
      { format: "jpeg", width: w, height: h, ok: problems.empty?, problems: problems }
    end

    def gif(b)
      ok = b.rstrip.end_with?(";")
      { format: "gif", width: u16le(b, 6), height: u16le(b, 8), ok: ok, problems: ok ? [] : ["no GIF trailer"] }
    end
  end
end
