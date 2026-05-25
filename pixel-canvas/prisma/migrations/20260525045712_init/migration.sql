-- CreateTable
CREATE TABLE "users" (
    "id" TEXT NOT NULL,
    "nickname" VARCHAR(50) NOT NULL,
    "github_id" VARCHAR(50),
    "total_pixels" INTEGER NOT NULL DEFAULT 0,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "users_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "pixel_history" (
    "id" SERIAL NOT NULL,
    "x" INTEGER NOT NULL,
    "y" INTEGER NOT NULL,
    "color" VARCHAR(7) NOT NULL,
    "user_id" TEXT NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "pixel_history_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "canvas_config" (
    "key" VARCHAR(50) NOT NULL,
    "value" TEXT NOT NULL,

    CONSTRAINT "canvas_config_pkey" PRIMARY KEY ("key")
);

-- CreateIndex
CREATE UNIQUE INDEX "users_github_id_key" ON "users"("github_id");

-- CreateIndex
CREATE INDEX "pixel_history_x_y_idx" ON "pixel_history"("x", "y");

-- CreateIndex
CREATE INDEX "pixel_history_created_at_idx" ON "pixel_history"("created_at");

-- AddForeignKey
ALTER TABLE "pixel_history" ADD CONSTRAINT "pixel_history_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
