const themeToggle = document.querySelector('.theme-toggle');
const helloButton = document.querySelector('#hello-button');
const year = document.querySelector('#year');

year.textContent = new Date().getFullYear();

let video_uuid = ""
let right_ascention = 0.0
let declination = 0.0
let pixel_size = 255

document.getElementById("submitButton").addEventListener('click', getImagesFromBackend());

document.getElementById("right_ascention").addEventListener('input',  ()=> {document.getElementById("right_ascention").value = right_ascention})

document.getElementById("right_ascention").addEventListener('input',  ()=> {document.getElementById("right_ascention").
value = declination})

document.getElementById("pixel_size_slider").addEventListner('input', () => {document.getElementById("pixel_size_slider").value = pixel_size})

function getImagesFromBackend(){
    video_uuid = await middleware.sendtoBackend(declination,right_ascention,pixel_size);
}
