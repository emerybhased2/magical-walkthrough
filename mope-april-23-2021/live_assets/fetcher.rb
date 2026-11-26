# frozen_string_literal: true

require "net/http"
require "uri"
require "openssl"

module LiveAssets
  class Throttled < StandardError; end

  # A polite HTTPS client for one site: a global request rate shared by all worker threads,
  # retries with back-off (Retry-After honoured) for 429/5xx, and a circuit breaker that stops
  # the run when the site keeps refusing (so a throttled production site is not hammered).
  class Fetcher
    USER_AGENT = "mope-local-archive/1.0 (local preservation; polite crawler)"

    attr_reader :requests

    # rate_per_second: 0 means no global rate cap (the number of workers is the only limit).
    def initialize(rate_per_second: 3.0, retries: 4, timeout: 60, max_refusals: 10, log: ->(m) { warn m })
      @interval = rate_per_second.to_f.positive? ? 1.0 / rate_per_second : 0.0
      @retries = retries
      @timeout = timeout
      @max_refusals = max_refusals
      @log = log
      @lock = Mutex.new
      @next_at = Time.now.to_f
      @refusals = 0
      @requests = 0
      proxy = ENV["HTTPS_PROXY"] || ENV["https_proxy"]
      @proxy = proxy && !proxy.empty? ? URI(proxy) : nil
    end

    def wait_turn
      at = @lock.synchronize do
        slot = [@next_at, Time.now.to_f].max
        @next_at = slot + @interval
        @requests += 1
        slot
      end
      delay = at - Time.now.to_f
      sleep(delay) if delay.positive?
    end

    # One kept-alive connection per worker thread and host.
    def session(uri)
      key = "#{uri.scheme}://#{uri.host}:#{uri.port}"
      pool = (Thread.current[:live_assets_http] ||= {})
      http = pool[key]
      return http if http&.started?
      http = connection(uri)
      http.keep_alive_timeout = 30
      http.start
      pool[key] = http
    end

    def drop_session(uri)
      pool = Thread.current[:live_assets_http] || {}
      http = pool.delete("#{uri.scheme}://#{uri.host}:#{uri.port}")
      http&.finish if http&.started?
    rescue StandardError
      nil
    end

    def close_sessions
      (Thread.current[:live_assets_http] || {}).each_value { |h| h.finish if h.started? rescue nil }
      Thread.current[:live_assets_http] = {}
    end

    def connection(uri)
      http = if @proxy
               Net::HTTP.new(uri.host, uri.port, @proxy.host, @proxy.port, @proxy.user, @proxy.password)
             else
               Net::HTTP.new(uri.host, uri.port, nil)
             end
      http.use_ssl = uri.scheme == "https"
      http.open_timeout = 20
      http.read_timeout = @timeout
      http
    end

    # => [status (Integer), content_type, body (binary String), final_url]
    def get(url)
      uri = URI(url)
      attempt = 0
      loop do
        raise Throttled, "the site refused #{@refusals} requests in a row; stopping to avoid overloading it" if @refusals >= @max_refusals
        wait_turn
        begin
          response = session(uri).request(Net::HTTP::Get.new(uri.request_uri, "User-Agent" => USER_AGENT, "Accept-Encoding" => "identity"))
        rescue Net::OpenTimeout, Net::ReadTimeout, Errno::ECONNRESET, Errno::ECONNREFUSED, Errno::EPIPE, IOError,
               OpenSSL::SSL::SSLError, EOFError, SocketError, Net::HTTPBadResponse => e
          drop_session(uri)
          attempt += 1
          raise if attempt > @retries
          @log.call("  network error (#{e.class}) for #{url}; retry #{attempt}")
          sleep([2**attempt, 60].min)
          next
        end
        code = response.code.to_i
        if code == 429 || code == 403 || code >= 500
          @lock.synchronize { @refusals += 1 } if code == 429 || code == 403
          attempt += 1
          raise Throttled, "HTTP #{code} for #{url}" if attempt > @retries && code != 403
          return [code, response["content-type"].to_s, response.body.to_s.b, url] if code == 403 && attempt > 1
          retry_after = response["retry-after"].to_i
          pause = retry_after.positive? ? [retry_after, 120].min : [5 * 2**attempt, 120].min
          @log.call("  HTTP #{code} for #{url}; waiting #{pause}s")
          sleep(pause)
          next
        end
        @lock.synchronize { @refusals = 0 }
        return [code, response["content-type"].to_s, response.body.to_s.b, url]
      end
    end
  end
end
