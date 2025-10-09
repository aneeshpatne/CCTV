import ffmpeg

ESP_URL = "http://192.168.1.116:81/stream" 

(
    ffmpeg
    .input(
        ESP_URL,
        fflags='nobuffer',
        flags='low_delay',
        use_wallclock_as_timestamps=1,
        reconnect=1,
        reconnect_streamed=1,
        reconnect_delay_max=2
    )
    .filter('fps', fps=20)
    .filter('scale', 1280, 720, flags='bicubic')
    .output(
        'tcp://0.0.0.0:9000?listen=1',
        f='mpegts',
        vcodec='libx264',
        preset='veryfast',
        tune='zerolatency',
        pix_fmt='yuv420p',
        g=40,
        b='1.2M',
        maxrate='1.5M',
        bufsize='2M'
    )
    .overwrite_output()
    .run()
)