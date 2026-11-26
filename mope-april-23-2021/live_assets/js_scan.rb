# frozen_string_literal: true

# Text-level scanning of minified JavaScript (bracket matching that skips strings and
# template literals). Nothing here evaluates JavaScript.
module LiveAssets
  module JsScan
    PAIRS = { "{" => "}", "(" => ")", "[" => "]" }.freeze
    CLOSERS = PAIRS.values.freeze

    module_function

    # Index of the closing quote of the string literal that opens at `i`.
    def skip_string(src, i)
      quote = src[i]
      k = i + 1
      while k < src.length
        c = src[k]
        return k if c == quote
        k += c == "\\" ? 2 : 1
      end
      src.length - 1
    end

    # Index of the closing backtick of the template literal that opens at `i` (handles ${ ... }).
    def skip_template(src, i)
      k = i + 1
      while k < src.length
        c = src[k]
        if c == "\\"
          k += 2
          next
        end
        return k if c == "`"
        if c == "$" && src[k + 1] == "{"
          close = find_close(src, k + 1)
          return src.length - 1 unless close
          k = close + 1
          next
        end
        k += 1
      end
      src.length - 1
    end

    # Index of the bracket matching the opener at `i`, or nil.
    def find_close(src, i)
      opener = src[i]
      raise ArgumentError, "not an opening bracket: #{opener.inspect}" unless PAIRS.key?(opener)
      stack = [PAIRS[opener]]
      j = i + 1
      while j < src.length
        c = src[j]
        if c == '"' || c == "'"
          j = skip_string(src, j)
        elsif c == "`"
          j = skip_template(src, j)
        elsif PAIRS.key?(c)
          stack.push(PAIRS[c])
        elsif CLOSERS.include?(c)
          return nil unless stack.last == c
          stack.pop
          return j if stack.empty?
        end
        j += 1
      end
      nil
    end

    # Top-level "key: value" pairs of an object literal's text (including its braces).
    def top_level_props(object_text)
      props = {}
      body_start = object_text.index("{")
      return props unless body_start
      j = body_start + 1
      depth = 0
      segment_start = j
      finish = object_text.length - 1
      while j <= finish
        c = object_text[j]
        if c == '"' || c == "'"
          j = skip_string(object_text, j)
        elsif c == "`"
          j = skip_template(object_text, j)
        elsif PAIRS.key?(c)
          depth += 1
        elsif CLOSERS.include?(c)
          if depth.zero?
            add_prop(props, object_text[segment_start...j])
            break
          end
          depth -= 1
        elsif c == "," && depth.zero?
          add_prop(props, object_text[segment_start...j])
          segment_start = j + 1
        end
        j += 1
      end
      props
    end

    def add_prop(props, segment)
      m = segment.match(/\A\s*([A-Za-z_$][\w$]*|`[^`]*`|"[^"]*")\s*:(.*)\z/m)
      props[m[1].delete("`\"")] = m[2].strip if m
    end
  end
end
