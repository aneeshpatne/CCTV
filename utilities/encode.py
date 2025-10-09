import ffmpeg

ESP_URL = "http://192.168.1.116:81/stream"

(
    ffmpeg
    .input(
        ESP_URL,
        fflags='nobuffer',
        flags='low_delay',
        probesize='32k', analyzeduration=0,
        use_wallclock_as_timestamps=1,
        reconnect=1, reconnect_streamed=1, reconnect_delay_max=2
    )
    .filter('fps', fps=20) 
    .filter('scale', 1280, 720, flags='bicubic')
    .output(
        'udp://127.0.0.1:9000?pkt_size=1316',
        f='mpegts',
        vcodec='libx264',
        preset='ultrafast',          
        tune='zerolatency',
        pix_fmt='yuv420p',
        g=40,                         
        x264opts='bframes=0:scenecut=0:keyint=40:min-keyint=40',  
        b='1.2M', maxrate='1.5M', bufsize='2M',
        muxdelay=0, muxpreload=0     
    )
    .global_args('-thread_queue_size', '1024') 
    .overwrite_output()
    .run()
)
