"""Batas waktu dan percobaan ulang panggilan RPC. Tanpa jaringan.

Keputusan operator M6.0: timeout eksplisit untuk menyambung **dan** membaca,
percobaan terbatas dengan jeda bertambah, dan tidak ada jalur yang menunggu
tanpa batas.

Yang diuji di sini adalah pembungkus percobaan di :class:`Web3Client`, dengan
aksi yang diberikan tes sendiri. Klien dibuat dengan URL yang menunjuk port 1
di loopback dan tidak satu pun tes memanggil method RPC-nya, jadi tidak ada
soket yang pernah dibuka.

:meth:`Web3Client._call` memang privat, dan diuji langsung dengan sengaja: ia
satu-satunya tempat keputusan "apa yang boleh diulang" diambil, dan
mengujinya lewat method publik berarti menguji sembilan jalan ke satu
keputusan yang sama.
"""

from __future__ import annotations

import pytest
from web3.exceptions import Web3Exception

from payment_guard.chain import (
    DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_RPC_READ_TIMEOUT_SECONDS,
    RPC_BACKOFF_SECONDS,
    RPC_MAX_ATTEMPTS,
    ChainError,
    Web3Client,
)

#: URL yang tidak pernah dihubungi. Port 1 di loopback.
DEAD_URL = "http://127.0.0.1:1/tidak-pernah-dihubungi"


class FakeSleep:
    """Mencatat jeda alih-alih menunggunya. Tes tidak boleh benar-benar tidur."""

    def __init__(self) -> None:
        self.waits: list[int] = []

    def __call__(self, seconds: int) -> None:
        self.waits.append(seconds)


def client_with(sleep: FakeSleep, *, max_attempts: int = RPC_MAX_ATTEMPTS) -> Web3Client:
    return Web3Client(DEAD_URL, max_attempts=max_attempts, sleep=sleep)


def transport_failure() -> int:
    """Kegagalan transport: kita tidak tahu apakah permintaannya sampai."""
    raise OSError("connection timed out")


class TestTimeouts:
    def test_connect_and_read_timeouts_are_separate(self) -> None:
        """``requests`` menerima pasangan (connect, read).

        Satu angka untuk keduanya berarti menunggu selama batas baca hanya
        untuk tahu bahwa portnya tertutup.
        """
        client = client_with(FakeSleep())
        kwargs = client._web3.provider.get_request_kwargs()
        assert kwargs["timeout"] == (
            DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS,
            DEFAULT_RPC_READ_TIMEOUT_SECONDS,
        )

    def test_the_connect_timeout_is_shorter(self) -> None:
        assert DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS < DEFAULT_RPC_READ_TIMEOUT_SECONDS

    def test_both_timeouts_are_integers(self) -> None:
        """Tidak ada ``float`` di ``src/``, termasuk di angka waktu."""
        assert isinstance(DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS, int)
        assert isinstance(DEFAULT_RPC_READ_TIMEOUT_SECONDS, int)

    def test_custom_timeouts_reach_the_provider(self) -> None:
        client = Web3Client(DEAD_URL, connect_timeout_seconds=2, read_timeout_seconds=7)
        assert client._web3.provider.get_request_kwargs()["timeout"] == (2, 7)

    def test_the_total_wait_for_one_call_is_bounded(self) -> None:
        """Batas atas satu panggilan bisa dihitung, dan kecil.

        Tiga percobaan, masing-masing paling lama connect + read, ditambah
        jeda di antaranya. Tidak ada suku yang tak berbatas.
        """
        per_attempt = DEFAULT_RPC_CONNECT_TIMEOUT_SECONDS + DEFAULT_RPC_READ_TIMEOUT_SECONDS
        batas = RPC_MAX_ATTEMPTS * per_attempt + sum(RPC_BACKOFF_SECONDS)
        assert batas <= 120, f"satu panggilan RPC bisa menggantung {batas} detik"


class TestRetryLimits:
    def test_a_transport_failure_is_retried_to_the_limit(self) -> None:
        sleep = FakeSleep()
        attempts: list[int] = []

        def gagal() -> int:
            attempts.append(1)
            return transport_failure()

        with pytest.raises(ChainError, match="setelah 3 percobaan"):
            client_with(sleep)._call("eth_chainId", gagal)

        assert len(attempts) == RPC_MAX_ATTEMPTS == 3

    def test_the_backoff_comes_from_the_table(self) -> None:
        sleep = FakeSleep()
        with pytest.raises(ChainError):
            client_with(sleep)._call("eth_chainId", transport_failure)
        # Dua jeda untuk tiga percobaan.
        assert sleep.waits == list(RPC_BACKOFF_SECONDS)

    def test_the_backoff_grows(self) -> None:
        assert list(RPC_BACKOFF_SECONDS) == sorted(RPC_BACKOFF_SECONDS)
        assert RPC_BACKOFF_SECONDS[0] < RPC_BACKOFF_SECONDS[-1]

    def test_it_succeeds_on_a_later_attempt(self) -> None:
        sleep = FakeSleep()
        sisa = [OSError("sekali gagal")]

        def kadang_gagal() -> str:
            if sisa:
                raise sisa.pop()
            return "berhasil"

        assert client_with(sleep)._call("eth_chainId", kadang_gagal) == "berhasil"
        assert sleep.waits == [RPC_BACKOFF_SECONDS[0]], "hanya satu jeda yang perlu"

    def test_a_first_attempt_success_never_sleeps(self) -> None:
        sleep = FakeSleep()
        assert client_with(sleep)._call("eth_chainId", lambda: 84532) == 84532
        assert sleep.waits == []

    def test_one_attempt_means_no_retry(self) -> None:
        sleep = FakeSleep()
        attempts: list[int] = []

        def gagal() -> int:
            attempts.append(1)
            return transport_failure()

        with pytest.raises(ChainError, match="setelah 1 percobaan"):
            client_with(sleep, max_attempts=1)._call("eth_chainId", gagal)
        assert len(attempts) == 1
        assert sleep.waits == []

    def test_the_backoff_table_is_never_indexed_past_its_end(self) -> None:
        """``max_attempts`` yang besar tidak boleh menyebabkan IndexError."""
        sleep = FakeSleep()
        with pytest.raises(ChainError):
            client_with(sleep, max_attempts=6)._call("eth_chainId", transport_failure)
        assert len(sleep.waits) == 5
        assert sleep.waits[-1] == RPC_BACKOFF_SECONDS[-1]


class TestWhatIsNotRetried:
    """Hanya kegagalan transport yang diulang. Jawaban tetap jawaban."""

    def test_a_protocol_rejection_is_not_retried(self) -> None:
        """Node sudah menjawab dan menolak; percobaan lagi menolak lagi."""
        sleep = FakeSleep()
        attempts: list[int] = []

        def ditolak() -> int:
            attempts.append(1)
            raise Web3Exception("nonce too low")

        with pytest.raises(ChainError, match="ditolak"):
            client_with(sleep)._call("eth_sendRawTransaction", ditolak)

        assert len(attempts) == 1, "jawaban protokol tidak boleh diulang"
        assert sleep.waits == []

    @pytest.mark.parametrize("kelas", [ValueError, KeyError])
    def test_malformed_answers_are_not_retried(self, kelas: type[Exception]) -> None:
        sleep = FakeSleep()
        attempts: list[int] = []

        def rusak() -> int:
            attempts.append(1)
            raise kelas("bentuk jawaban salah")

        with pytest.raises(ChainError):
            client_with(sleep)._call("eth_getBalance", rusak)
        assert len(attempts) == 1
        assert sleep.waits == []

    def test_a_chain_error_passes_through_unchanged(self) -> None:
        sleep = FakeSleep()

        def menolak() -> int:
            raise ChainError("alasan kami sendiri")

        with pytest.raises(ChainError, match="alasan kami sendiri"):
            client_with(sleep)._call("decimals()", menolak)
        assert sleep.waits == []

    def test_requests_transport_errors_are_os_errors(self) -> None:
        """Penjaga asumsi: retry dipilih dengan menangkap ``OSError``.

        ``requests.exceptions.RequestException`` turun dari ``IOError``, yang
        adalah ``OSError``. Kalau itu berubah di versi mendatang, kegagalan
        transport akan berhenti diulang tanpa ada yang memberi tahu.
        """
        import requests

        assert issubclass(requests.exceptions.RequestException, OSError)
        assert issubclass(requests.exceptions.Timeout, OSError)
        assert issubclass(requests.exceptions.ConnectionError, OSError)


class TestFailureMessages:
    def test_the_message_never_leaks_the_url(self) -> None:
        """``RPC_URL`` rahasia: kunci API biasa ada di path atau query."""
        client = Web3Client(
            "https://rpc.example/v2/kunci-rahasia?token=juga-rahasia",
            max_attempts=1,
            sleep=FakeSleep(),
        )
        with pytest.raises(ChainError) as info:
            client._call("eth_chainId", transport_failure)
        pesan = str(info.value)
        assert "kunci-rahasia" not in pesan
        assert "juga-rahasia" not in pesan
        assert "https://rpc.example" in pesan

    def test_the_message_names_the_call(self) -> None:
        with pytest.raises(ChainError, match="eth_getBalance"):
            client_with(FakeSleep(), max_attempts=1)._call("eth_getBalance", transport_failure)

    def test_the_message_names_the_attempt_count(self) -> None:
        with pytest.raises(ChainError, match="3 percobaan"):
            client_with(FakeSleep())._call("eth_chainId", transport_failure)
